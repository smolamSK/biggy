"""Schema export/import, adopted tables and multi-source. (Split from test_features.py.)"""
import io
import json
import os

from sqlalchemy import select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    MetaForm,
    MetaPermission,
    MetaTable,
    Webhook,
)
from tests.helpers import (
    _add_field,
    _fid,
    _home_tables,
    _make_existing_tables,
    _make_form,
    _make_form_p,
    _make_source,
    _make_source_generic,
    _make_table,
    _ok,
    _scalar,
    _setup,
)


def test_schema_export_preserves_flags_and_permissions(app, client):
    _setup(client)
    tid = _make_table(client, app, "note", "Note", "body")
    fid = _make_form(client, app, "note_form", "Notes", tid)
    _ok(client.post(f"/designer/tables/{tid}/flags",
                    data=dict(track_audit="y", soft_delete="y"), follow_redirects=True))
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "read"}, follow_redirects=True))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    payload = exp.get_data()
    data = json.loads(payload)
    tbl = next(t for t in data["tables"] if t["phys_name"] == "note")
    assert tbl["track_audit"] and tbl["soft_delete"]
    assert any(p["access"] == "read" for p in data["permissions"])

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(payload), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        t = s.scalar(select(MetaTable).where(MetaTable.phys_name == "note"))
        assert t.track_audit and t.soft_delete
        assert s.scalar(select(MetaPermission).where(MetaPermission.access == "read")) is not None
        with get_engine().connect() as c:
            cols = {row[0] for row in c.execute(text("SHOW COLUMNS FROM note")).all()}
        assert {"created_by", "created_at", "deleted_at"} <= cols

def test_schema_export_has_all_sections(app, client):
    """Every config section must be present in the export (regression guard)."""
    _setup(client)
    _make_table(client, app, "thing", "Thing", "name")
    data = json.loads(client.get("/designer/schema/export.json").get_data())
    assert {"tables", "fields", "relations", "forms", "form_fields", "menus",
            "permissions", "workflows", "trigger_rules", "roles", "field_permissions",
            "composite_uniques", "connections", "feeds", "webhooks", "pull_sources",
            "dashboards", "dashboard_widgets", "sequences"} <= set(data)

def test_autonumber_sequence_survives_roundtrip(app, client):
    from app.metadata.models import Sequence
    _setup(client)
    tid = _make_table(client, app, "invoice", "Invoice", "title")
    _add_field(client, tid, "number", "autonumber", default_value="INV-")
    fid = _make_form(client, app, "invoice_form", "Invoices", tid)
    for t in ("A", "B"):
        _ok(client.post(f"/u/forms/{fid}/new", data={"title": t}, follow_redirects=True))

    # the counter (now at 3) is carried in the export
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    payload = exp.get_data()
    assert any(s.get("next") == 3 for s in json.loads(payload).get("sequences", []))

    # replace-import wipes data but restores the counter, so numbering continues
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(payload), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        assert SessionLocal().scalar(select(Sequence)).next == 3
        nfid = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "invoice_form")).id

    _ok(client.post(f"/u/forms/{nfid}/new", data={"title": "C"}, follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            nums = [r[0] for r in c.execute(text("SELECT number FROM invoice ORDER BY id")).all()]
    assert nums == ["INV-0003"]      # continued, not restarted at INV-0001

def test_adopt_existing_tables(app, client):
    from app import adopt
    from app.metadata.models import MetaRelation
    _setup(client)
    _make_existing_tables(app)

    # introspection: dept/emp/note adoptable; legacy is not (composite primary key)
    with app.app_context():
        cand = {x["name"]: x for x in adopt.list_adoptable(SessionLocal(), get_engine())}
    assert cand["dept"]["ok"] and cand["emp"]["ok"] and cand["note"]["ok"]
    assert not cand["legacy"]["ok"] and "primary key" in cand["legacy"]["reason"].lower()

    # adopt via the designer UI, importing foreign keys as relations
    _ok(client.post("/designer/adopt",
                    data={"tables": ["dept", "emp", "note"], "with_relations": "y"},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        emp = s.scalar(select(MetaTable).where(MetaTable.phys_name == "emp"))
        dept = s.scalar(select(MetaTable).where(MetaTable.phys_name == "dept"))
        assert emp.managed is False and dept.managed is False
        cols = {f.phys_name: f.data_type for f in emp.fields}
        assert cols.get("name") == "string" and cols.get("salary") == "decimal"
        assert cols.get("dept_id") == "relation" and "id" not in cols   # FK→relation, id implicit
        rel = s.scalar(select(MetaRelation).where(MetaRelation.kind == "m1"))
        assert rel is not None and rel.to_table_id == dept.id
        emp_tid, note_tid = emp.id, s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "note")).id

    # forms work on the adopted table: create + list through /u
    fid = _make_form(client, app, "emp_form", "Employees", emp_tid)
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "Ada", "salary": "1000.00", "dept_id": "1"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            row = c.execute(text("SELECT name, salary, dept_id FROM emp LIMIT 1")).mappings().first()
    assert row["name"] == "Ada" and float(row["salary"]) == 1000.0 and row["dept_id"] == 1
    assert "Ada" in client.get(f"/u/forms/{fid}").get_data(as_text=True)

    # DDL guard: adding a column to an external table is refused
    cols_sql = ("SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name='emp' AND table_schema=DATABASE()")
    before = _scalar(app, cols_sql)
    client.post(f"/designer/tables/{emp_tid}/fields",
                data=dict(phys_name="extra", label="Extra", data_type="string", nullable="y"),
                follow_redirects=True)
    assert _scalar(app, cols_sql) == before                 # no column added

    # unmapping a (relation-free) external table keeps the real table + its rows
    _ok(client.post(f"/designer/tables/{note_tid}/delete", follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "note")) is None
    assert _scalar(app, "SELECT COUNT(*) FROM note") == 0   # physical table still exists

def test_adopt_backup_safety(app, client):
    from app import adopt
    _setup(client)
    _make_existing_tables(app)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO emp (id, name, salary) VALUES (1, 'Bo', 9.00)"))
        s = SessionLocal()
        adopt.adopt_table(s, get_engine(), "emp")
        s.commit()

    exp = client.get("/designer/schema/export.json").get_data()
    emp_t = next(t for t in json.loads(exp)["tables"] if t["phys_name"] == "emp")
    assert emp_t["managed"] is False                         # flag round-trips

    # a replace-import wipes Biggy tables but must NOT drop the external one
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    assert _scalar(app, "SELECT COUNT(*) FROM emp") == 1     # external rows survive
    with app.app_context():
        assert SessionLocal().scalar(
            select(MetaTable).where(MetaTable.phys_name == "emp")).managed is False

def test_data_source_table_lives_elsewhere(app, client, src2):
    _setup(client)
    sid = _make_source(app, client, src2)
    _ok(client.post("/designer/tables/new",
                    data={"phys_name": "widget", "label": "Widget", "source_id": str(sid)},
                    follow_redirects=True))
    with app.app_context():
        mt = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "widget"))
        assert mt.source_id == sid
        tid = mt.id
    _add_field(client, tid, "name", "string", length="80")
    fid = _make_form(client, app, "widget_form", "Widgets", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "W1"}, follow_redirects=True))

    with src2["engine"].connect() as c:                  # rows live in biggy_test2
        assert [r[0] for r in c.execute(text("SELECT name FROM widget")).all()] == ["W1"]
    assert "widget" not in _home_tables(app)             # ...not in the home database
    assert "W1" in client.get(f"/u/forms/{fid}").get_data(as_text=True)

def test_relation_same_source_guard(app, client, src2):
    from app.metadata.models import MetaRelation
    _setup(client)
    home_tid = _make_table(client, app, "customer", "Customer", "name")
    sid = _make_source(app, client, src2)
    _ok(client.post("/designer/tables/new",
                    data={"phys_name": "ordr", "label": "Order", "source_id": str(sid)},
                    follow_redirects=True))
    with app.app_context():
        ordr_tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "ordr")).id

    r = client.post("/designer/relations/new-m1", data=dict(
        name="oc", from_table_id=ordr_tid, to_table_id=home_tid, field_name="customer_id",
        on_delete="SET NULL", nullable="y"), follow_redirects=True)
    assert "same data source" in r.get_data(as_text=True)
    with app.app_context():
        assert SessionLocal().scalar(select(MetaRelation)) is None       # not created

def test_adopt_from_other_source(app, client, src2):
    _setup(client)
    sid = _make_source(app, client, src2)
    with src2["engine"].begin() as c:                    # a pre-existing table in biggy_test2
        c.execute(text("CREATE TABLE gadget (id INT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(80))"))
        c.execute(text("INSERT INTO gadget (id, name) VALUES (1, 'G1')"))

    _ok(client.post("/designer/adopt",
                    data={"source": str(sid), "tables": ["gadget"], "with_relations": "y"},
                    follow_redirects=True))
    with app.app_context():
        mt = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "gadget"))
        assert mt and mt.managed is False and mt.source_id == sid
        gid = mt.id
    _make_form_p(client, app, "gadget_view", "Gadget", gid, "view")
    assert "G1" in client.get(f"/u/view/{gid}/1").get_data(as_text=True)
    assert "gadget" not in _home_tables(app)             # never created in home

def test_multisource_backup_roundtrip(app, client, src2):
    _setup(client)
    sid = _make_source(app, client, src2)
    _ok(client.post("/designer/tables/new",
                    data={"phys_name": "widget", "label": "Widget", "source_id": str(sid)},
                    follow_redirects=True))
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "widget")).id
    _add_field(client, tid, "name", "string", length="80")
    fid = _make_form(client, app, "widget_form", "Widgets", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "W1"}, follow_redirects=True))

    schema = client.get("/designer/schema/export.json").get_data()
    data = client.get("/designer/data/export.json").get_data()
    sj = json.loads(schema)
    assert next(t for t in sj["tables"] if t["phys_name"] == "widget")["source_id"] is not None
    assert sj["data_sources"] and sj["data_sources"][0]["database"] == "biggy_test2"

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(schema), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    _ok(client.post("/designer/data/import",
                    data={"file": (io.BytesIO(data), "d.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))

    with src2["engine"].connect() as c:                  # recreated in biggy_test2 with its row
        assert [r[0] for r in c.execute(text("SELECT name FROM widget")).all()] == ["W1"]
    assert "widget" not in _home_tables(app)             # home database untouched

def test_sqlite_backend_full_ddl(app, client, sqlite_source):
    from sqlalchemy import inspect as sqla_inspect
    _setup(client)
    sid = _make_source_generic(app, client, sqlite_source["params"], "sqlitedb")

    # a managed table ON the SQLite source
    _ok(client.post("/designer/tables/new",
                    data={"phys_name": "person", "label": "Person", "source_id": str(sid)},
                    follow_redirects=True))
    with app.app_context():
        ptid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "person")).id

    _add_field(client, ptid, "name", "string", length="80")
    _add_field(client, ptid, "email", "string", length="120", is_unique="y")  # unique index
    _add_field(client, ptid, "age", "integer")

    # modify_column: rename age -> years (batch table-rebuild on SQLite)
    age_fid = _fid(app, "person", "age")
    _ok(client.post(f"/designer/tables/{ptid}/fields/{age_fid}/edit",
                    data=dict(phys_name="years", label="Years", data_type="integer", nullable="y"),
                    follow_redirects=True))
    # composite unique on (name, years)
    nid, yid = _fid(app, "person", "name"), _fid(app, "person", "years")
    _ok(client.post(f"/designer/tables/{ptid}/uniques",
                    data={"field_ids": [str(nid), str(yid)]}, follow_redirects=True))
    # a second table + an M:1 relation (add_relation_column → batch FK on SQLite)
    _ok(client.post("/designer/tables/new",
                    data={"phys_name": "company", "label": "Company", "source_id": str(sid)},
                    follow_redirects=True))
    with app.app_context():
        ctid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "company")).id
    _ok(client.post("/designer/relations/new-m1", data=dict(
        name="Employer", from_table_id=ptid, to_table_id=ctid, field_name="company_id",
        on_delete="SET NULL", nullable="y"), follow_redirects=True))
    # drop_column: drop email (its unique index goes too)
    email_fid = _fid(app, "person", "email")
    _ok(client.post(f"/designer/tables/{ptid}/fields/{email_fid}/delete", follow_redirects=True))

    # every DDL op is reflected in the physical SQLite schema
    insp = sqla_inspect(sqlite_source["engine"])
    cols = {c["name"] for c in insp.get_columns("person")}
    assert cols == {"id", "name", "years", "company_id"}
    assert any(fk["constrained_columns"] == ["company_id"] for fk in insp.get_foreign_keys("person"))
    assert any(ix["unique"] and set(ix["column_names"]) == {"name", "years"}
               for ix in insp.get_indexes("person"))

    # full CRUD through the normal /u routes against the SQLite-backed table
    pf = _make_form(client, app, "person_form", "People", ptid)
    _ok(client.post(f"/u/forms/{pf}/new", data={"name": "Ada", "years": "30"},
                    follow_redirects=True))
    with sqlite_source["engine"].connect() as c:
        assert c.execute(text("SELECT name, years FROM person")).all() == [("Ada", 30)]
    assert "Ada" in client.get(f"/u/forms/{pf}").get_data(as_text=True)

def test_schema_reference_example_imports(app, client):
    """The documented reference schema (docs/schema-reference.example.json) must import
    cleanly — this pins docs/schema-json-format.md's example to the real importer."""
    from app.metadata.models import Dashboard, MetaMenu, MetaRelation, MetaTable, TriggerRule
    _setup(client)
    path = os.path.join(os.path.dirname(__file__), "..", "docs", "schema-reference.example.json")
    with open(path, "rb") as fh:
        data = fh.read()
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(data), "ref.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        order = s.scalar(select(MetaTable).where(MetaTable.phys_name == "sales_order"))
        assert order is not None and order.pk_col == "ref"           # custom-PK table
        assert s.scalar(select(MetaRelation).where(MetaRelation.kind == "m1")) is not None
        assert s.scalar(select(MetaRelation).where(MetaRelation.kind == "mn")) is not None
        dash = s.scalar(select(Dashboard).where(Dashboard.name == "Overview"))
        assert dash is not None and len(dash.widgets) == 4           # chart/number/list/text
        menu = s.scalar(select(MetaMenu).where(MetaMenu.kind == "dashboard"))
        assert menu.target_dashboard_id == dash.id                   # menu→dashboard remapped
        assert s.scalar(select(TriggerRule).where(TriggerRule.event == "scheduled")) is not None
        assert s.scalar(select(Webhook)) is not None
        with get_engine().connect() as c:                            # m1 FK column was created
            n = c.execute(text("SELECT COUNT(*) FROM information_schema.columns WHERE "
                               "table_name='sales_order' AND column_name='customer_id' "
                               "AND table_schema=DATABASE()")).scalar()
        assert n == 1
