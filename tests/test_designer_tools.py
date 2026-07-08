"""Designer productivity tools: form generation, CSV wizard, duplication."""
import io

from sqlalchemy import select, text

from app.db import SessionLocal, get_engine
from app.identifiers import sanitize_identifier
from app.importer import infer_schema
from app.metadata.models import MetaForm, MetaMenu, MetaTable
from tests.helpers import _add_field, _make_form, _make_table, _ok, _setup


# --------------------------------------------------------------------------- #
# Pure unit tests (no DB)
# --------------------------------------------------------------------------- #
def test_sanitize_identifier():
    assert sanitize_identifier("First Name") == "first_name"
    assert sanitize_identifier("Prix (EUR)") == "prix_eur"
    assert sanitize_identifier("2nd col") == "c_2nd_col"
    assert sanitize_identifier("id") == "x_id"           # reserved column dodged
    assert sanitize_identifier("app_x") == "x_app_x"     # reserved prefix dodged
    assert sanitize_identifier("") == "col"
    assert sanitize_identifier("---") == "col"
    assert len(sanitize_identifier("x" * 200)) <= 60


def test_infer_schema():
    csv_text = ("Name,Qty,Price,Active,Since,Name\n"
                "Ann,1,9.50,yes,2024-01-05,a\n"
                "Bob,2,12,no,2024-02-06,\n")
    cols, samples, n = infer_schema(csv_text)
    assert n == 2 and len(samples) == 2
    by = {c["name"]: c for c in cols}
    assert by["name"]["data_type"] == "string"
    assert by["qty"]["data_type"] == "integer"
    assert by["price"]["data_type"] == "decimal"         # 9.50 + 12 mix
    assert by["active"]["data_type"] == "boolean"
    assert by["since"]["data_type"] == "date"
    assert "name_2" in by                                # duplicate header de-duped


# --------------------------------------------------------------------------- #
# Integration (biggy_test)
# --------------------------------------------------------------------------- #
def test_generate_form_and_add_all(app, client):
    _setup(client)
    tid = _make_table(client, app, "asset", "Asset", "name")
    _add_field(client, tid, "qty", "integer")
    other = _make_table(client, app, "tagx", "TagX", "name")
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Tags", from_table_id=tid, to_table_id=other),
                    follow_redirects=True))

    r = client.post(f"/designer/tables/{tid}/generate-form",
                    data={"with_view": "y", "with_menu": "y"}, follow_redirects=True)
    _ok(r)
    with app.app_context():
        s = SessionLocal()
        mf = s.scalar(select(MetaForm).where(MetaForm.name == "asset_form"))
        vf = s.scalar(select(MetaForm).where(MetaForm.name == "asset_view"))
        assert mf is not None and vf is not None and vf.purpose == "view"
        assert len(mf.items) == 3                        # name + qty + m:n relation
        assert {i.kind for i in mf.items} == {"field", "relation"}
        assert s.scalar(select(MetaMenu).where(MetaMenu.target_form_id == mf.id)) is not None
        mf_id = mf.id

    # the generated form really works end-to-end
    _ok(client.post(f"/u/forms/{mf_id}/new", data={"name": "Laptop", "qty": "3"},
                    follow_redirects=True))
    assert "Laptop" in client.get(f"/u/forms/{mf_id}").get_data(as_text=True)

    # add-all fills an empty form and is idempotent
    _ok(client.post("/designer/forms/new",
                    data=dict(name="asset2", title="Asset 2", table_id=tid),
                    follow_redirects=True))
    with app.app_context():
        fid2 = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "asset2")).id
    _ok(client.post(f"/designer/forms/{fid2}/add-all", follow_redirects=True))
    _ok(client.post(f"/designer/forms/{fid2}/add-all", follow_redirects=True))
    with app.app_context():
        assert len(SessionLocal().get(MetaForm, fid2).items) == 3


def test_table_from_csv(app, client):
    _setup(client)
    csv_text = ("Item Name,Qty,Price,Active,Since\n"
                "Widget,1,9.50,yes,2024-01-05\n"
                "Gadget,2,12.00,no,2024-02-06\n")

    # step 1: upload → review shows sanitized names + sniffed types
    r = client.post("/designer/tables/from-csv",
                    data={"file": (io.BytesIO(csv_text.encode()), "inv.csv"),
                          "label": "Inventory"},
                    content_type="multipart/form-data")
    html = r.get_data(as_text=True)
    assert 'value="item_name"' in html
    assert "decimal" in html and "boolean" in html

    # step 2: create — drop 'since', keep the sniffed types
    data = {"step": "create", "csv_text": csv_text,
            "phys_name": "inventory", "label": "Inventory"}
    for i, (name, typ) in enumerate([("item_name", "string"), ("qty", "integer"),
                                     ("price", "decimal"), ("active", "boolean"),
                                     ("since", "date")]):
        if name != "since":
            data[f"include_{i}"] = "y"
        data[f"name_{i}"] = name
        data[f"label_{i}"] = name.title()
        data[f"type_{i}"] = typ
    _ok(client.post("/designer/tables/from-csv", data=data, follow_redirects=True))

    with app.app_context():
        s = SessionLocal()
        mt = s.scalar(select(MetaTable).where(MetaTable.phys_name == "inventory"))
        assert {f.phys_name for f in mt.fields} == {"item_name", "qty", "price", "active"}
        assert mt.display_field_id is not None           # first text column
        with get_engine().connect() as c:
            rows = c.execute(text(
                "SELECT item_name, qty, active FROM inventory ORDER BY id")).all()
        assert [(r[0], r[1], bool(r[2])) for r in rows] == \
            [("Widget", 1, True), ("Gadget", 2, False)]


def test_duplicate_table_and_form(app, client):
    _setup(client)
    tid = _make_table(client, app, "device", "Device", "name")
    _add_field(client, tid, "cost", "decimal")
    fid = _make_form(client, app, "device_form", "Devices", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "Router", "cost": "9"},
                    follow_redirects=True))

    _ok(client.post(f"/designer/tables/{tid}/duplicate",
                    data={"phys_name": "device2"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        mt2 = s.scalar(select(MetaTable).where(MetaTable.phys_name == "device2"))
        assert mt2 is not None and mt2.label == "Device (copy)"
        assert {f.phys_name for f in mt2.fields} == {"name", "cost"}
        with get_engine().connect() as c:                # structure only, no data
            assert c.execute(text("SELECT COUNT(*) FROM device2")).scalar() == 0

    _ok(client.post(f"/designer/forms/{fid}/duplicate", follow_redirects=True))
    with app.app_context():
        f2 = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "device_form_copy"))
        assert f2 is not None and len(f2.items) == 2 and f2.table_id == tid
