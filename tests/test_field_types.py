"""Field types, formulas, defaults, uniques and custom PKs. (Split from test_features.py.)"""
import io
import json
from datetime import date

from sqlalchemy import select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    MetaField,
    MetaTable,
)
from tests.helpers import (
    _add_field,
    _make_form,
    _make_form_p,
    _make_source_generic,
    _make_table,
    _mint,
    _ok,
    _scalar,
    _setup,
)


def test_typed_text_fields(app, client):
    from app import importer
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    _add_field(client, tid, "email", "email")
    _add_field(client, tid, "site", "url")
    _add_field(client, tid, "phone", "phone")
    fid = _make_form(client, app, "lead_form", "Leads", tid)
    _make_form_p(client, app, "lead_view", "Lead", tid, "view")

    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "Ann", "email": "ann@x.com", "site": "https://acme.test",
                          "phone": "+1 555 1234"}, follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM lead LIMIT 1")).scalar()

    lh = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "mailto:ann@x.com" in lh
    assert 'href="https://acme.test"' in lh
    assert "tel:+15551234" in lh
    vh = client.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)
    assert "mailto:ann@x.com" in vh

    # an invalid email is rejected by the form (re-render, no row written)
    r = client.post(f"/u/forms/{fid}/new", data={"name": "Bad", "email": "not-an-email"})
    assert r.status_code == 200
    with app.app_context():
        n = get_engine().connect().execute(
            text("SELECT COUNT(*) FROM lead WHERE name='Bad'")).scalar()
    assert n == 0

    # coerce_value (CSV / inline path) validates the format too
    with app.app_context():
        fmeta = SessionLocal().scalar(select(MetaField).where(MetaField.phys_name == "email"))
        raised = False
        try:
            importer.coerce_value(fmeta, "nope")
        except ValueError:
            raised = True
        assert raised
        assert importer.coerce_value(fmeta, "ok@y.com") == "ok@y.com"

def test_currency_percent_formatting(app, client):
    _setup(client)
    tid = _make_table(client, app, "product", "Product", "name")
    _add_field(client, tid, "price", "currency", precision="12", scale="2")
    _add_field(client, tid, "margin", "percent", precision="6", scale="2")
    fid = _make_form(client, app, "product_form", "Products", tid)
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "Widget", "price": "1234.5", "margin": "12.5"},
                    follow_redirects=True))
    lh = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "$1,234.50" in lh
    assert "12.5%" in lh

def test_json_and_tags_fields(app, client):
    _setup(client)
    tid = _make_table(client, app, "item", "Item", "name")
    _add_field(client, tid, "spec", "json")
    _add_field(client, tid, "labels", "tags", enum_options="red\ngreen\nblue")
    fid = _make_form(client, app, "item_form", "Items", tid)

    # invalid JSON is rejected (re-render, no row)
    r = client.post(f"/u/forms/{fid}/new", data={"name": "X", "spec": "{not json"})
    assert r.status_code == 200
    with app.app_context():
        assert get_engine().connect().execute(
            text("SELECT COUNT(*) FROM item WHERE name='X'")).scalar() == 0

    # canonical JSON stored; multiple tags stored as a JSON array
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "Y", "spec": '{"a":  1}', "labels": ["red", "blue"]},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            row = c.execute(text("SELECT spec, labels FROM item WHERE name='Y'")).mappings().first()
    assert json.loads(row["spec"]) == {"a": 1}
    assert set(json.loads(row["labels"])) == {"red", "blue"}

    # tags render as chips in the list
    lh = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert lh.count("badge") >= 2

    # CSV "a|b" imports to two tags
    csv_text = "name,labels\nZed,red|green\n"
    _ok(client.post(f"/u/import/{tid}",
                    data={"file": (io.BytesIO(csv_text.encode()), "i.csv"), "mode": "insert"},
                    content_type="multipart/form-data"))
    with app.app_context():
        v = get_engine().connect().execute(
            text("SELECT labels FROM item WHERE name='Zed'")).scalar()
    assert set(json.loads(v)) == {"red", "green"}

def test_autonumber_sequence(app, client):
    _setup(client)
    tid = _make_table(client, app, "invoice", "Invoice", "title")
    _add_field(client, tid, "number", "autonumber", default_value="INV-")
    fid = _make_form(client, app, "invoice_form", "Invoices", tid)

    # field is rendered read-only on the new-record form
    nh = client.get(f"/u/forms/{fid}/new").get_data(as_text=True)
    assert 'name="number"' in nh and "readonly" in nh

    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "A"}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "B"}, follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            nums = [r[0] for r in c.execute(text("SELECT number FROM invoice ORDER BY id")).all()]
    assert nums == ["INV-0001", "INV-0002"]

def test_default_expressions(app, client):
    _setup(client)
    tid = _make_table(client, app, "memo", "Memo", "title")
    _add_field(client, tid, "created_on", "date", default_value="today")
    _add_field(client, tid, "owner", "string", default_value="current_user", length="80")
    fid = _make_form(client, app, "memo_form", "Memos", tid)

    # via the form
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "viaform"}, follow_redirects=True))
    # via the REST API (same create chokepoint)
    H = _mint(app, "boss")
    api = app.test_client()
    r = api.post("/api/v1/memo", json={"title": "viaapi"}, headers=H)
    assert r.status_code == 201, r.get_data(as_text=True)

    today = date.today().isoformat()
    with app.app_context():
        with get_engine().connect() as c:
            rows = c.execute(
                text("SELECT title, created_on, owner FROM memo")).mappings().all()
    by = {row["title"]: row for row in rows}
    for title in ("viaform", "viaapi"):
        assert str(by[title]["created_on"]) == today
        assert by[title]["owner"] == "boss"

def test_composite_unique(app, client):
    from app.metadata.models import CompositeUnique
    _setup(client)
    tid = _make_table(client, app, "enrol", "Enrol", "name")
    _add_field(client, tid, "student", "integer")
    _add_field(client, tid, "course", "integer")
    with app.app_context():
        fids = {f.phys_name: f.id for f in SessionLocal().get(MetaTable, tid).fields}

    # need at least two columns
    _ok(client.post(f"/designer/tables/{tid}/uniques",
                    data={"field_ids": [str(fids["student"])]}, follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(CompositeUnique)) is None

    _ok(client.post(f"/designer/tables/{tid}/uniques",
                    data={"field_ids": [str(fids["student"]), str(fids["course"])]},
                    follow_redirects=True))
    with app.app_context():
        cu = SessionLocal().scalar(select(CompositeUnique))
        assert cu is not None and len(json.loads(cu.field_ids)) == 2

    def _dup_blocked():
        with app.app_context():
            eng = get_engine()
            with eng.begin() as c:
                c.execute(text("INSERT INTO enrol (name, student, course) VALUES ('a',1,2)"))
            with eng.begin() as c:                      # a different pair is fine
                c.execute(text("INSERT INTO enrol (name, student, course) VALUES ('c',1,3)"))
            blocked = False
            try:
                with eng.begin() as c:
                    c.execute(text("INSERT INTO enrol (name, student, course) VALUES ('b',1,2)"))
            except Exception:                           # noqa: BLE001 - IntegrityError
                blocked = True
            return blocked

    assert _dup_blocked()

    # the constraint survives schema export/import (replace recreates the table)
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    payload = exp.get_data()
    data = json.loads(payload)
    assert any(len(json.loads(u["field_ids"])) == 2 for u in data["composite_uniques"])
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(payload), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        assert SessionLocal().scalar(select(CompositeUnique)) is not None
    assert _dup_blocked()

def test_view_page_with_m2n_relation(app, client):
    """A many-to-many relation shown on a view form must render (regression: the
    'items' dict key collided with dict.items in Jinja, 500-ing the view page)."""
    from app.metadata.models import MetaRelation
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    tag_tid = _make_table(client, app, "tag", "Tag", "name")
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Tags", from_table_id=cust_tid, to_table_id=tag_tid),
                    follow_redirects=True))
    with app.app_context():
        rel_id = SessionLocal().scalar(select(MetaRelation).where(MetaRelation.kind == "mn")).id

    vfid = _make_form_p(client, app, "customer_view", "Customer", cust_tid, "view")
    _ok(client.post(f"/designer/forms/{vfid}",
                    data=dict(kind="relation", relation_id=rel_id), follow_redirects=True))

    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO customer (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO tag (id, name) VALUES (1,'vip')"))
            c.execute(text("INSERT INTO j_customer_tag (customer_id, tag_id) VALUES (1,1)"))

    v = client.get(f"/u/view/{cust_tid}/1")
    _ok(v)                                        # previously raised a 500
    body = v.get_data(as_text=True)
    assert "Acme" in body and "vip" in body       # the linked tag is listed

def test_formula_engine():
    from decimal import Decimal

    from app import formula as F

    class Stub:
        def lookup(self, rel, fld):
            return {"region": "EU"}.get(fld)

        def rollup(self, rel, fld, op="count"):
            return {"count": 3, "sum": 60}.get(op)

    ev, stub = F.evaluate, Stub()
    assert ev("qty * price", {"qty": 3, "price": Decimal("2.5")}) == Decimal("7.5")
    assert ev("(price-cost)/price*100", {"price": Decimal("10"), "cost": Decimal("7")}) == Decimal("30")
    assert ev("first & ' ' & last", {"first": "Ada", "last": "Lovelace"}) == "Ada Lovelace"
    assert ev("a if a > b else b", {"a": 5, "b": 9}) == 9
    assert ev("today() - created", {"created": date.today()}) == 0
    assert ev("x / 0", {"x": 5}) is None                       # div-by-zero → None
    assert ev("coalesce(a, b, 0)", {"a": None, "b": None}) == 0
    assert ev("lookup('customer_id','region')", {}, stub) == "EU"
    assert ev("rollup('orders','total','sum')", {}, stub) == 60

    assert F.validate("qty * price", {"qty", "price"}) is None
    assert F.validate("qty * bogus", {"qty", "price"})                 # unknown field
    assert F.validate('__import__("os")', {"qty"})                     # unsafe call
    assert F.validate("a.b", {"a"})                                    # attribute access
    assert F.validate("lookup('customer_id','x')", {"customer_id"},
                      lookup_fields={"customer_id"}) is None
    assert F.validate("lookup('nope','x')", {"customer_id"}, lookup_fields={"customer_id"})

    assert F.coerce_result(0, "boolean") is False
    assert F.coerce_result(date(2020, 1, 2), "date") == date(2020, 1, 2)

def test_formula_same_record(app, client):
    _setup(client)
    tid = _make_table(client, app, "line", "Line", "name")
    _add_field(client, tid, "qty", "integer")
    _add_field(client, tid, "price", "decimal", precision="10", scale="2")
    _add_field(client, tid, "total", "formula", formula="qty * price", result_type="number")
    fid = _make_form(client, app, "line_form", "Lines", tid)

    nf = client.get(f"/u/forms/{fid}/new").get_data(as_text=True)
    assert 'name="total"' in nf and "readonly" in nf       # read-only on the form

    # a posted value for the formula column is ignored; it is computed
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "A", "qty": "3", "price": "2.50", "total": "999"},
                    follow_redirects=True))
    pk = _scalar(app, "SELECT id FROM line LIMIT 1")
    assert float(_scalar(app, "SELECT total FROM line WHERE id=:i", i=pk)) == 7.5

    _ok(client.post(f"/u/forms/{fid}/{pk}/edit",
                    data={"name": "A", "qty": "4", "price": "2.50"}, follow_redirects=True))
    assert float(_scalar(app, "SELECT total FROM line WHERE id=:i", i=pk)) == 10.0  # recomputed

    lh = client.get(f"/u/forms/{fid}?sort=total&order=desc")          # sortable real column
    _ok(lh)
    assert "10" in lh.get_data(as_text=True)

def test_formula_lookup_ripple(app, client):
    _setup(client)
    cust = _make_table(client, app, "customer", "Customer", "name")
    _add_field(client, cust, "region", "string", length="40")
    order = _make_table(client, app, "ordr", "Order", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="OrderCustomer", from_table_id=order, to_table_id=cust,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _add_field(client, order, "cust_region", "formula",
               formula="lookup('customer_id', 'region')", result_type="text")
    cfid = _make_form(client, app, "cust_form", "Customers", cust)
    ofid = _make_form(client, app, "order_form", "Orders", order)

    _ok(client.post(f"/u/forms/{cfid}/new", data={"name": "Acme", "region": "EU"},
                    follow_redirects=True))
    cid = _scalar(app, "SELECT id FROM customer LIMIT 1")
    _ok(client.post(f"/u/forms/{ofid}/new", data={"code": "O1", "customer_id": str(cid)},
                    follow_redirects=True))
    assert _scalar(app, "SELECT cust_region FROM ordr LIMIT 1") == "EU"     # lookup on create

    _ok(client.post(f"/u/forms/{cfid}/{cid}/edit", data={"name": "Acme", "region": "US"},
                    follow_redirects=True))
    assert _scalar(app, "SELECT cust_region FROM ordr LIMIT 1") == "US"     # ripple to child

def test_formula_rollup_ripple(app, client):
    _setup(client)
    acct = _make_table(client, app, "account", "Account", "name")
    order = _make_table(client, app, "ordr", "Order", "code")
    _add_field(client, order, "amount", "decimal", precision="10", scale="2")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Orders", from_table_id=order, to_table_id=acct,
                              field_name="account_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _add_field(client, acct, "n_orders", "formula",
               formula="rollup('Orders', '', 'count')", result_type="number")
    _add_field(client, acct, "total", "formula",
               formula="rollup('Orders', 'amount', 'sum')", result_type="number")
    afid = _make_form(client, app, "acct_form", "Accounts", acct)
    ofid = _make_form(client, app, "order_form", "Orders", order)

    _ok(client.post(f"/u/forms/{afid}/new", data={"name": "A"}, follow_redirects=True))
    aid = _scalar(app, "SELECT id FROM account LIMIT 1")

    def acct_row():
        with app.app_context():
            with get_engine().connect() as c:
                return c.execute(text("SELECT n_orders, total FROM account WHERE id=:i"),
                                 {"i": aid}).mappings().first()

    for code, amt in (("O1", "10.00"), ("O2", "5.00")):
        _ok(client.post(f"/u/forms/{ofid}/new",
                        data={"code": code, "amount": amt, "account_id": str(aid)},
                        follow_redirects=True))
    r = acct_row()
    assert int(r["n_orders"]) == 2 and float(r["total"]) == 15.0    # ripple on add

    opk = _scalar(app, "SELECT id FROM ordr ORDER BY id LIMIT 1")
    _ok(client.post(f"/u/forms/{ofid}/{opk}/edit",
                    data={"code": "O1", "amount": "20.00", "account_id": str(aid)},
                    follow_redirects=True))
    assert float(acct_row()["total"]) == 25.0                        # ripple on edit

    _ok(client.post(f"/u/forms/{ofid}/{opk}/delete", follow_redirects=True))
    r = acct_row()
    assert int(r["n_orders"]) == 1 and float(r["total"]) == 5.0      # ripple on delete

def test_formula_schema_data_roundtrip(app, client):
    from app.metadata.models import MetaField
    _setup(client)
    tid = _make_table(client, app, "line", "Line", "name")
    _add_field(client, tid, "qty", "integer")
    _add_field(client, tid, "price", "decimal", precision="10", scale="2")
    _add_field(client, tid, "total", "formula", formula="qty * price", result_type="number")
    fid = _make_form(client, app, "line_form", "Lines", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "A", "qty": "3", "price": "2.00"},
                    follow_redirects=True))

    schema = client.get("/designer/schema/export.json").get_data()
    data = client.get("/designer/data/export.json").get_data()
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(schema), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    _ok(client.post("/designer/data/import",
                    data={"file": (io.BytesIO(data), "d.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        f = SessionLocal().scalar(select(MetaField).where(MetaField.phys_name == "total"))
        assert f.formula == "qty * price" and f.result_type == "number"
    assert float(_scalar(app, "SELECT total FROM line LIMIT 1")) == 6.0   # value preserved

def test_custom_pk_table(app, client):
    _setup(client)
    _ok(client.post("/designer/tables/new", data={
        "phys_name": "product", "label": "Product", "source_id": "0",
        "pk_mode": "custom", "pk_name": "code", "pk_type": "string", "pk_length": "20"},
        follow_redirects=True))
    with app.app_context():
        mt = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "product"))
        assert mt.pk_col == "code"
        assert any(f.phys_name == "code" and not f.nullable for f in mt.fields)  # required key field
        tid = mt.id
    _add_field(client, tid, "name", "string", length="80")
    _make_form_p(client, app, "product_view", "Product", tid, "view")
    fid = _make_form(client, app, "product_form", "Products", tid)

    # create supplying the natural key
    _ok(client.post(f"/u/forms/{fid}/new", data={"code": "SKU1", "name": "Widget"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            row = c.execute(text("SELECT code, name FROM product")).mappings().first()
    assert row["code"] == "SKU1" and row["name"] == "Widget"

    # view/list/edit/delete all key on the string PK
    v = client.get(f"/u/view/{tid}/SKU1")
    _ok(v)
    assert "Widget" in v.get_data(as_text=True)
    assert f"/u/view/{tid}/SKU1" in client.get(f"/u/forms/{fid}").get_data(as_text=True)
    _ok(client.post(f"/u/forms/{fid}/SKU1/edit", data={"code": "SKU1", "name": "Widget2"},
                    follow_redirects=True))
    assert _scalar(app, "SELECT name FROM product WHERE code='SKU1'") == "Widget2"
    _ok(client.post(f"/u/forms/{fid}/SKU1/delete", follow_redirects=True))
    assert _scalar(app, "SELECT COUNT(*) FROM product") == 0

def test_relation_to_string_pk_target(app, client):
    _setup(client)
    _ok(client.post("/designer/tables/new", data={
        "phys_name": "product", "label": "Product", "source_id": "0",
        "pk_mode": "custom", "pk_name": "code", "pk_type": "string", "pk_length": "20"},
        follow_redirects=True))
    with app.app_context():
        pid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "product")).id
    _add_field(client, pid, "name", "string", length="80")
    oid = _make_table(client, app, "ordr", "Order", "ref")
    _ok(client.post("/designer/relations/new-m1", data=dict(
        name="prod", from_table_id=oid, to_table_id=pid, field_name="product_code",
        on_delete="SET NULL", nullable="y"), follow_redirects=True))
    pf = _make_form(client, app, "product_form", "Products", pid)
    of = _make_form(client, app, "order_form", "Orders", oid)

    _ok(client.post(f"/u/forms/{pf}/new", data={"code": "SKU1", "name": "Widget"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{of}/new", data={"ref": "O1", "product_code": "SKU1"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT product_code FROM ordr LIMIT 1")).scalar() == "SKU1"

def test_adopt_string_pk_on_sqlite(app, client, sqlite_source):
    _setup(client)
    sid = _make_source_generic(app, client, sqlite_source["params"], "sqlitedb")
    with sqlite_source["engine"].begin() as c:
        c.execute(text("CREATE TABLE account (uuid VARCHAR(36) PRIMARY KEY, name VARCHAR(80))"))

    _ok(client.post("/designer/adopt", data={"source": str(sid), "tables": ["account"]},
                    follow_redirects=True))
    with app.app_context():
        mt = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "account"))
        assert mt and mt.managed is False and mt.pk_col == "uuid"
        cols = {f.phys_name: f for f in mt.fields}
        assert "uuid" in cols and not cols["uuid"].nullable     # natural key exposed + required
        aid = mt.id
    _make_form_p(client, app, "account_view", "Account", aid, "view")
    af = _make_form(client, app, "account_form", "Accounts", aid)

    _ok(client.post(f"/u/forms/{af}/new", data={"uuid": "u-1", "name": "Acme"},
                    follow_redirects=True))
    with sqlite_source["engine"].connect() as c:
        assert c.execute(text("SELECT name FROM account WHERE uuid='u-1'")).scalar() == "Acme"
    assert "Acme" in client.get(f"/u/view/{aid}/u-1").get_data(as_text=True)   # string-key view URL


def test_markdown_field(app, client):
    """Markdown fields store text, render HTML on view pages, and neutralize raw HTML."""
    _setup(client)
    tid = _make_table(client, app, "article", "Article", "title")
    _add_field(client, tid, "body", "markdown")
    fid = _make_form(client, app, "article_form", "Articles", tid)
    _make_form_p(client, app, "article_view", "Article", tid, "view")

    body = "**Bold move** and a list:\n\n- one\n- two\n\n<script>alert('xss')</script>"
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "A1", "body": body},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            stored = c.execute(text("SELECT body FROM article WHERE id=1")).scalar()
    assert stored == body                                   # stored as plain text

    vh = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "<strong>Bold move</strong>" in vh               # rendered
    assert "<li>one</li>" in vh
    assert "<script>alert" not in vh                        # raw HTML neutralized
    assert "&lt;script&gt;" in vh

    # searchable via global search (markdown is a text-like type)
    assert "A1" in client.get("/u/search?q=Bold+move").get_data(as_text=True)
