"""Full-stack flow through the Flask test client against the live test DB."""
import io
import json

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import MetaForm, MetaMenu, MetaRelation, MetaTable


def _ok(resp):
    assert resp.status_code < 400, resp.get_data(as_text=True)[:400]


def test_relation_display_fields(app, client):
    """Designer picks multiple display fields; User-mode picker shows composite labels."""
    def tbl(phys):
        with app.app_context():
            return SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))

    # customer (name + email) and order (code) with an M:1 order -> customer
    _ok(client.post("/designer/tables/new", data=dict(phys_name="customer", label="Customer"),
                    follow_redirects=True))
    cust = tbl("customer")
    for f in [dict(phys_name="name", label="Name", data_type="string", length=80, nullable="y"),
              dict(phys_name="email", label="Email", data_type="string", length=120, nullable="y")]:
        _ok(client.post(f"/designer/tables/{cust.id}/fields", data=f, follow_redirects=True))

    _ok(client.post("/designer/tables/new", data=dict(phys_name="order", label="Order"),
                    follow_redirects=True))
    order = tbl("order")
    _ok(client.post(f"/designer/tables/{order.id}/fields",
                    data=dict(phys_name="code", label="Code", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order.id, to_table_id=cust.id,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))

    with app.app_context():
        s = SessionLocal()
        rel_id = s.scalar(select(MetaRelation).where(MetaRelation.kind == "m1")).id
        cust_fields = {f.phys_name: f.id
                       for f in s.scalar(select(MetaTable)
                                         .where(MetaTable.phys_name == "customer")).fields}

    # choose name + email as the display fields for this relation
    _ok(client.post(f"/designer/relations/{rel_id}/edit",
                    data={"name": "Order customer",
                          "to_display_field_ids": [str(cust_fields["name"]),
                                                   str(cust_fields["email"])]},
                    follow_redirects=True))
    with app.app_context():
        rel = SessionLocal().get(MetaRelation, rel_id)
        assert json.loads(rel.to_display_field_ids) == [cust_fields["name"], cust_fields["email"]]

    # order form with code + customer_id
    _ok(client.post("/designer/forms/new",
                    data=dict(name="order_form", title="Orders", table_id=order.id),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        of_id = s.scalar(select(MetaForm).where(MetaForm.name == "order_form")).id
        ofields = {f.phys_name: f.id
                   for f in s.scalar(select(MetaTable)
                                     .where(MetaTable.phys_name == "order")).fields}
    for name in ("code", "customer_id"):
        _ok(client.post(f"/designer/forms/{of_id}",
                        data=dict(kind="field", field_id=ofields[name]), follow_redirects=True))

    with app.app_context():
        with get_engine().begin() as conn:
            conn.execute(text("INSERT INTO customer (name, email) VALUES ('Acme', 'a@acme.test')"))

    resp = client.get(f"/u/forms/{of_id}/new")
    _ok(resp)
    assert "Acme — a@acme.test" in resp.get_data(as_text=True)


def test_user_mode_column_filters(app, client):
    """Add-condition builder filters the list via parallel fcol/fop/fval params."""
    def tbl(phys):
        with app.app_context():
            return SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="product", label="Product"),
                    follow_redirects=True))
    product = tbl("product")
    for f in [dict(phys_name="name", label="Name", data_type="string", length=60, nullable="y"),
              dict(phys_name="price", label="Price", data_type="decimal", precision=10, scale=2,
                   nullable="y")]:
        _ok(client.post(f"/designer/tables/{product.id}/fields", data=f, follow_redirects=True))

    _ok(client.post("/designer/forms/new",
                    data=dict(name="product_form", title="Products", table_id=product.id),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        of_id = s.scalar(select(MetaForm).where(MetaForm.name == "product_form")).id
        pfields = {f.phys_name: f.id
                   for f in s.scalar(select(MetaTable)
                                     .where(MetaTable.phys_name == "product")).fields}
    for name in ("name", "price"):
        _ok(client.post(f"/designer/forms/{of_id}",
                        data=dict(kind="field", field_id=pfields[name]), follow_redirects=True))

    with app.app_context():
        with get_engine().begin() as conn:
            conn.execute(text("INSERT INTO product (name, price) VALUES "
                              "('Widget', 10), ('Gadget', 20), ('Gizmo', 5)"))

    # the Filters builder is present
    page = client.get(f"/u/forms/{of_id}")
    _ok(page)
    body = page.get_data(as_text=True)
    assert 'id="filter-meta"' in body and "Add condition" in body

    # two conditions (AND): name contains 'g' AND price >= 10  ->  Widget, Gadget (not Gizmo)
    resp = client.get(f"/u/forms/{of_id}",
                      query_string="fcol=name&fop=contains&fval=g"
                                   "&fcol=price&fop=gte&fval=10")
    _ok(resp)
    html = resp.get_data(as_text=True)
    assert "Widget" in html and "Gadget" in html
    assert "Gizmo" not in html


def test_menu_groups_are_collapsible(app, client):
    """A menu group renders as a collapsible <details> with expand/collapse-all controls."""
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="thing", label="Thing"),
                    follow_redirects=True))
    with app.app_context():
        thing_id = SessionLocal().scalar(
            select(MetaTable).where(MetaTable.phys_name == "thing")).id
    _ok(client.post(f"/designer/tables/{thing_id}/fields",
                    data=dict(phys_name="name", label="Name", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/forms/new",
                    data=dict(name="thing_form", title="Things", table_id=thing_id),
                    follow_redirects=True))
    with app.app_context():
        form_id = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "thing_form")).id

    _ok(client.post("/designer/menus/new",
                    data=dict(label="Section", kind="group", position=0), follow_redirects=True))
    with app.app_context():
        group_id = SessionLocal().scalar(select(MetaMenu).where(MetaMenu.kind == "group")).id
    _ok(client.post("/designer/menus/new",
                    data=dict(label="Things", kind="form", parent_id=group_id,
                              target_form_id=form_id, position=0), follow_redirects=True))

    resp = client.get("/u/")
    _ok(resp)
    html = resp.get_data(as_text=True)
    assert 'class="menu-group"' in html
    assert f'data-menu-id="{group_id}"' in html
    assert 'id="menu-expand-all"' in html and 'id="menu-collapse-all"' in html


def test_designer_field_edit_reorder_validation_defaults(app, client):
    """Field edit/rename + reorder, default pre-fill, and validation enforcement."""
    def fields_of(phys):
        with app.app_context():
            t = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))
            return {f.phys_name: f.id for f in t.fields}, t.id

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="person", label="Person"),
                    follow_redirects=True))
    _, person_id = fields_of("person")
    _ok(client.post(f"/designer/tables/{person_id}/fields",
                    data=dict(phys_name="fname", label="First name", data_type="string",
                              length=40, nullable="y", default_value="guest"),
                    follow_redirects=True))
    _ok(client.post(f"/designer/tables/{person_id}/fields",
                    data=dict(phys_name="age", label="Age", data_type="integer", nullable="y",
                              min_value="0", max_value="120"), follow_redirects=True))
    fmap, _ = fields_of("person")

    _ok(client.post("/designer/forms/new",
                    data=dict(name="person_form", title="People", table_id=person_id),
                    follow_redirects=True))
    with app.app_context():
        pf_id = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "person_form")).id
    for n in ("fname", "age"):
        _ok(client.post(f"/designer/forms/{pf_id}",
                        data=dict(kind="field", field_id=fmap[n]), follow_redirects=True))

    # default pre-fills the new-record form
    page = client.get(f"/u/forms/{pf_id}/new")
    _ok(page)
    assert 'value="guest"' in page.get_data(as_text=True)

    # validation: age 200 (> max 120) is rejected; 50 is accepted
    _ok(client.post(f"/u/forms/{pf_id}/new", data={"fname": "Bob", "age": "200"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{pf_id}/new", data={"fname": "Bob", "age": "50"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM person")).scalar() == 1

    # rename fname -> full_name, preserving data
    _ok(client.post(f"/designer/tables/{person_id}/fields/{fmap['fname']}/edit",
                    data=dict(data_type="string", phys_name="full_name", label="Full name",
                              length=40, nullable="y", default_value="guest"),
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as conn:
            cols = {c for (c, *_rest) in conn.execute(text("SHOW COLUMNS FROM person")).all()}
            assert "full_name" in cols and "fname" not in cols
            assert conn.execute(text("SELECT full_name FROM person")).scalar() == "Bob"

    # reorder: move age above the (renamed) first field
    _ok(client.post(f"/designer/tables/{person_id}/fields/{fmap['age']}/move/up",
                    follow_redirects=True))
    with app.app_context():
        t = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "person"))
        assert [f.phys_name for f in sorted(t.fields, key=lambda x: x.position)][0] == "age"


def test_designer_form_item_and_menu_reorder(app, client):
    """Form-item edit + reorder and menu reorder."""
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="thing", label="Thing"),
                    follow_redirects=True))
    with app.app_context():
        thing_id = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "thing")).id
    for n in ("a", "b"):
        _ok(client.post(f"/designer/tables/{thing_id}/fields",
                        data=dict(phys_name=n, label=n.upper(), data_type="string", length=20,
                                  nullable="y"), follow_redirects=True))
    with app.app_context():
        fmap = {f.phys_name: f.id for f in
                SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "thing")).fields}
    _ok(client.post("/designer/forms/new",
                    data=dict(name="thing_form", title="Things", table_id=thing_id),
                    follow_redirects=True))
    with app.app_context():
        tf_id = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "thing_form")).id
    for n in ("a", "b"):
        _ok(client.post(f"/designer/forms/{tf_id}",
                        data=dict(kind="field", field_id=fmap[n]), follow_redirects=True))
    with app.app_context():
        items = SessionLocal().scalar(select(MetaForm).where(MetaForm.id == tf_id)).items
        first_item_id = sorted(items, key=lambda i: i.position)[0].id

    # edit the first item: make it required with a label override
    _ok(client.post(f"/designer/forms/{tf_id}/items/{first_item_id}/edit",
                    data=dict(label_override="Field A", required="y"), follow_redirects=True))
    # move it down
    _ok(client.post(f"/designer/forms/{tf_id}/items/{first_item_id}/move/down",
                    follow_redirects=True))
    with app.app_context():
        from app.metadata.models import MetaFormField
        it = SessionLocal().get(MetaFormField, first_item_id)
        assert it.required and it.label_override == "Field A" and it.position == 1

    # two top-level menu items; move the second up
    for label in ("First", "Second"):
        _ok(client.post("/designer/menus/new",
                        data=dict(label=label, kind="group", position=0), follow_redirects=True))
    with app.app_context():
        menus = SessionLocal().scalars(
            select(MetaMenu).order_by(MetaMenu.id)).all()
        second_id = [m.id for m in menus if m.label == "Second"][0]
    _ok(client.post(f"/designer/menus/{second_id}/move/up", follow_redirects=True))
    with app.app_context():
        second = SessionLocal().get(MetaMenu, second_id)
        assert second.position == 0


def test_designer_diagram(app, client):
    """The ER-diagram page renders the schema graph (tables + relations) as JSON."""
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/examples/cmdb/load", follow_redirects=True))

    resp = client.get("/designer/diagram")
    _ok(resp)
    html = resp.get_data(as_text=True)
    assert 'id="er-graph"' in html and "diagram.js" in html

    blob = html.split('id="er-graph">', 1)[1].split("</script>", 1)[0]
    graph = json.loads(blob)
    assert len(graph["tables"]) == 4                       # cmdb: team/env/ci/application
    kinds = {r["kind"] for r in graph["relations"]}
    assert "m1" in kinds and "mn" in kinds
    assert any(f["pk"] for t in graph["tables"] for f in t["fields"])      # synthetic PK row
    assert any(f["fk_to"] for t in graph["tables"] for f in t["fields"])   # an FK field


def test_data_export_import_round_trip(app, client):
    """Export all data to JSON, mutate, then restore (replace) preserving ids/FKs."""
    def tbl(phys):
        with app.app_context():
            return SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="customer", label="Customer"),
                    follow_redirects=True))
    cust = tbl("customer")
    _ok(client.post(f"/designer/tables/{cust.id}/fields",
                    data=dict(phys_name="name", label="Name", data_type="string", length=60,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="tag", label="Tag"),
                    follow_redirects=True))
    tag = tbl("tag")
    _ok(client.post(f"/designer/tables/{tag.id}/fields",
                    data=dict(phys_name="name", label="Name", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="order", label="Order"),
                    follow_redirects=True))
    order = tbl("order")
    _ok(client.post(f"/designer/tables/{order.id}/fields",
                    data=dict(phys_name="code", label="Code", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order.id, to_table_id=cust.id,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Order tags", from_table_id=order.id, to_table_id=tag.id),
                    follow_redirects=True))

    with app.app_context():
        with get_engine().begin() as conn:
            conn.execute(text("INSERT INTO customer (id, name) VALUES (1,'Acme'),(2,'Globex')"))
            conn.execute(text("INSERT INTO tag (id, name) VALUES (1,'vip'),(2,'rush')"))
            conn.execute(text("INSERT INTO `order` (id, code, customer_id) "
                              "VALUES (10,'O-1',1),(11,'O-2',2)"))
            conn.execute(text("INSERT INTO j_order_tag (order_id, tag_id) "
                              "VALUES (10,1),(10,2),(11,1)"))

    exp = client.get("/designer/data/export.json")
    _ok(exp)
    payload = exp.get_data()
    data = json.loads(payload)
    assert data["version"] == 1
    assert len(data["tables"]["customer"]) == 2
    assert len(data["tables"]["order"]) == 2
    assert len(data["tables"]["j_order_tag"]) == 3

    with app.app_context():       # mutate before restoring
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM j_order_tag"))
            conn.execute(text("DELETE FROM `order`"))

    done = client.post("/designer/data/import",
                       data={"file": (io.BytesIO(payload), "data.json"), "replace_existing": "y"},
                       content_type="multipart/form-data")
    _ok(done)
    with app.app_context():
        with get_engine().connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM `order`")).scalar() == 2
            assert conn.execute(text("SELECT customer_id FROM `order` WHERE id=10")).scalar() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM j_order_tag")).scalar() == 3
            assert conn.execute(text("SELECT COUNT(*) FROM customer")).scalar() == 2

    # a payload table that isn't in the schema is reported as skipped
    bogus = {"version": 1, "tables": dict(data["tables"], nonexistent_tbl=[{"id": 1}])}
    resp = client.post("/designer/data/import",
                       data={"file": (io.BytesIO(json.dumps(bogus).encode()), "d.json"),
                             "replace_existing": "y"},
                       content_type="multipart/form-data")
    _ok(resp)
    assert "Skipped unknown tables: nonexistent_tbl" in resp.get_data(as_text=True)


def test_schema_export_import_round_trip(app, client):
    """Export the whole model to JSON, then re-import it (replace) and verify."""
    def tbl(phys):
        with app.app_context():
            return SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))

    def counts():
        with app.app_context():
            s = SessionLocal()
            return tuple(s.scalar(select(func.count()).select_from(m))
                        for m in (MetaTable, MetaRelation, MetaForm, MetaMenu))

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))

    # build a model: customer(name,email), tag(name), order(code) + M:1 + M:N
    _ok(client.post("/designer/tables/new", data=dict(phys_name="customer", label="Customer"),
                    follow_redirects=True))
    cust = tbl("customer")
    for f in [dict(phys_name="name", label="Name", data_type="string", length=80, nullable="y"),
              dict(phys_name="email", label="Email", data_type="string", length=120, nullable="y")]:
        _ok(client.post(f"/designer/tables/{cust.id}/fields", data=f, follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="tag", label="Tag"),
                    follow_redirects=True))
    tag = tbl("tag")
    _ok(client.post(f"/designer/tables/{tag.id}/fields",
                    data=dict(phys_name="name", label="Name", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/tables/new", data=dict(phys_name="order", label="Order"),
                    follow_redirects=True))
    order = tbl("order")
    _ok(client.post(f"/designer/tables/{order.id}/fields",
                    data=dict(phys_name="code", label="Code", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order.id, to_table_id=cust.id,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Order tags", from_table_id=order.id, to_table_id=tag.id),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        m1_id = s.scalar(select(MetaRelation).where(MetaRelation.kind == "m1")).id
        mn_id = s.scalar(select(MetaRelation).where(MetaRelation.kind == "mn")).id
        cust_fields = {f.phys_name: f.id for f in
                       s.scalar(select(MetaTable).where(MetaTable.phys_name == "customer")).fields}
    _ok(client.post(f"/designer/relations/{m1_id}/edit",
                    data={"name": "Order customer",
                          "to_display_field_ids": [str(cust_fields["name"]),
                                                   str(cust_fields["email"])]},
                    follow_redirects=True))
    _ok(client.post("/designer/forms/new",
                    data=dict(name="order_form", title="Orders", table_id=order.id),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        of_id = s.scalar(select(MetaForm).where(MetaForm.name == "order_form")).id
        ofields = {f.phys_name: f.id for f in
                   s.scalar(select(MetaTable).where(MetaTable.phys_name == "order")).fields}
    for name in ("code", "customer_id"):
        _ok(client.post(f"/designer/forms/{of_id}",
                        data=dict(kind="field", field_id=ofields[name]), follow_redirects=True))
    _ok(client.post(f"/designer/forms/{of_id}",
                    data=dict(kind="relation", relation_id=mn_id), follow_redirects=True))
    _ok(client.post("/designer/menus/new",
                    data=dict(label="Sales", kind="group", position=0), follow_redirects=True))
    with app.app_context():
        grp_id = SessionLocal().scalar(select(MetaMenu).where(MetaMenu.kind == "group")).id
    _ok(client.post("/designer/menus/new",
                    data=dict(label="Orders", kind="form", parent_id=grp_id, target_form_id=of_id,
                              position=0), follow_redirects=True))

    assert counts() == (3, 2, 1, 2)

    # export
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    payload = exp.get_data()
    data = json.loads(payload)
    assert data["version"] == 1
    assert len(data["tables"]) == 3 and len(data["relations"]) == 2
    assert len(data["forms"]) == 1 and len(data["menus"]) == 2
    assert any(f["data_type"] == "relation" for f in data["fields"])

    # importing without Replace must refuse (model already present)
    refused = client.post("/designer/schema/import",
                          data={"file": (io.BytesIO(payload), "schema.json")},
                          content_type="multipart/form-data")
    _ok(refused)
    assert "already contains a model" in refused.get_data(as_text=True)
    assert counts() == (3, 2, 1, 2)

    # import with Replace: wipe and recreate
    done = client.post("/designer/schema/import",
                       data={"file": (io.BytesIO(payload), "schema.json"), "replace_existing": "y"},
                       content_type="multipart/form-data")
    _ok(done)
    assert counts() == (3, 2, 1, 2)

    with app.app_context():
        s = SessionLocal()
        with get_engine().connect() as conn:
            names = {t for (t,) in conn.execute(text("SHOW TABLES")).all()}
        assert {"customer", "tag", "order", "j_order_tag"} <= names
        new_cust = s.scalar(select(MetaTable).where(MetaTable.phys_name == "customer"))
        cust_ids = {f.id for f in new_cust.fields}
        disp = json.loads(
            s.scalar(select(MetaRelation).where(MetaRelation.kind == "m1")).to_display_field_ids)
        assert disp and all(i in cust_ids for i in disp)   # display fields remapped to new ids
        new_form_id = s.scalar(select(MetaForm).where(MetaForm.name == "order_form")).id

    _ok(client.get(f"/u/forms/{new_form_id}"))   # imported model is usable


def test_csv_import(app, client):
    """Download a template, then import rows (all-or-nothing vs skip, relation by name)."""
    def tbl(phys):
        with app.app_context():
            return SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))

    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))

    _ok(client.post("/designer/tables/new", data=dict(phys_name="city", label="City"),
                    follow_redirects=True))
    city = tbl("city")
    _ok(client.post(f"/designer/tables/{city.id}/fields",
                    data=dict(phys_name="name", label="Name", data_type="string", length=60,
                              nullable="y"), follow_redirects=True))

    _ok(client.post("/designer/tables/new", data=dict(phys_name="customer", label="Customer"),
                    follow_redirects=True))
    cust = tbl("customer")
    _ok(client.post(f"/designer/tables/{cust.id}/fields",     # required (no nullable)
                    data=dict(phys_name="name", label="Name", data_type="string", length=80),
                    follow_redirects=True))
    _ok(client.post(f"/designer/tables/{cust.id}/fields",
                    data=dict(phys_name="email", label="Email", data_type="string", length=120,
                              nullable="y"), follow_redirects=True))
    _ok(client.post(f"/designer/tables/{cust.id}/fields",
                    data=dict(phys_name="active", label="Active", data_type="boolean",
                              nullable="y"), follow_redirects=True))
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Home city", from_table_id=cust.id, to_table_id=city.id,
                              field_name="city_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))

    with app.app_context():
        with get_engine().begin() as conn:
            conn.execute(text("INSERT INTO city (name) VALUES ('Paris')"))
        paris_id = get_engine().connect().execute(
            text("SELECT id FROM city WHERE name='Paris'")).scalar()

    tmpl = client.get(f"/u/import/{cust.id}/template.csv")
    _ok(tmpl)
    assert tmpl.get_data(as_text=True).splitlines()[0] == "name,email,active,city_id"

    good = ("name,email,active,city_id\n"
            "Acme,a@acme.test,yes,Paris\n"
            "Globex,b@globex.test,no,Paris\n")
    bad = good + "Bad,c@x.test,maybe,Paris\n"   # 'maybe' is not a boolean

    def upload(text_csv, skip):
        data = {"file": (io.BytesIO(text_csv.encode()), "data.csv")}
        if skip:
            data["skip_invalid"] = "y"
        return client.post(f"/u/import/{cust.id}", data=data,
                           content_type="multipart/form-data")

    def count():
        with app.app_context():
            return get_engine().connect().execute(
                text("SELECT COUNT(*) FROM customer")).scalar()

    _ok(upload(bad, skip=False))           # all-or-nothing: bad row blocks everything
    assert count() == 0

    _ok(upload(bad, skip=True))            # import the two valid rows, skip the bad one
    assert count() == 2
    with app.app_context():
        eng = get_engine()
        assert eng.connect().execute(
            text("SELECT city_id FROM customer WHERE name='Acme'")).scalar() == paris_id
        assert eng.connect().execute(
            text("SELECT active FROM customer WHERE name='Globex'")).scalar() in (0, False)


def test_full_designer_and_user_flow(app, client):
    def find_table(phys):
        with app.app_context():
            return SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys))

    # setup ------------------------------------------------------------------
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                         confirm="secret1"), follow_redirects=True))

    # tables + fields --------------------------------------------------------
    _ok(client.post("/designer/tables/new",
                    data=dict(phys_name="customer", label="Customer"), follow_redirects=True))
    cust = find_table("customer")
    for f in [dict(phys_name="name", label="Name", data_type="string", length=120, nullable="y"),
              dict(phys_name="active", label="Active", data_type="boolean")]:
        _ok(client.post(f"/designer/tables/{cust.id}/fields", data=f, follow_redirects=True))

    _ok(client.post("/designer/tables/new", data=dict(phys_name="tag", label="Tag"),
                    follow_redirects=True))
    tag = find_table("tag")
    _ok(client.post(f"/designer/tables/{tag.id}/fields",
                    data=dict(phys_name="name", label="Name", data_type="string", length=60,
                              nullable="y"), follow_redirects=True))

    _ok(client.post("/designer/tables/new", data=dict(phys_name="order", label="Order"),
                    follow_redirects=True))
    order = find_table("order")
    _ok(client.post(f"/designer/tables/{order.id}/fields",
                    data=dict(phys_name="code", label="Code", data_type="string", length=40,
                              nullable="y"), follow_redirects=True))

    # relations --------------------------------------------------------------
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order.id, to_table_id=cust.id,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Order tags", from_table_id=order.id, to_table_id=tag.id),
                    follow_redirects=True))

    with app.app_context():
        eng = get_engine()
        tables = {t for (t,) in eng.connect().execute(text("SHOW TABLES")).all()}
        assert "j_order_tag" in tables
        s = SessionLocal()
        mn = s.scalar(select(MetaRelation).where(MetaRelation.kind == "mn"))
        mn_id = mn.id

    # forms ------------------------------------------------------------------
    _ok(client.post("/designer/forms/new",
                    data=dict(name="order_form", title="Orders", table_id=order.id),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        order_form = s.scalar(select(MetaForm).where(MetaForm.name == "order_form"))
        of_id = order_form.id
        order_fields = {f.phys_name: f.id
                        for f in s.scalar(select(MetaTable)
                                          .where(MetaTable.phys_name == "order")).fields}
    for name in ("code", "customer_id"):
        _ok(client.post(f"/designer/forms/{of_id}",
                        data=dict(kind="field", field_id=order_fields[name]),
                        follow_redirects=True))
    _ok(client.post(f"/designer/forms/{of_id}",
                    data=dict(kind="relation", relation_id=mn_id), follow_redirects=True))

    # menu -------------------------------------------------------------------
    _ok(client.post("/designer/menus/new",
                    data=dict(label="Orders", kind="form", target_form_id=of_id),
                    follow_redirects=True))

    # user CRUD --------------------------------------------------------------
    with app.app_context():
        eng = get_engine()
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO customer (name, active) VALUES ('Acme', 1)"))
            conn.execute(text("INSERT INTO tag (name) VALUES ('vip'),('rush')"))
        cid = eng.connect().execute(text("SELECT id FROM customer LIMIT 1")).scalar()
        tag_ids = [r[0] for r in eng.connect().execute(text("SELECT id FROM tag")).all()]

    _ok(client.post(f"/u/forms/{of_id}/new",
                    data={"code": "ORD-1", "customer_id": str(cid),
                          f"rel_{mn_id}": [str(t) for t in tag_ids]}, follow_redirects=True))
    with app.app_context():
        eng = get_engine()
        oid = eng.connect().execute(text("SELECT id FROM `order` LIMIT 1")).scalar()
        assert eng.connect().execute(text("SELECT COUNT(*) FROM j_order_tag")).scalar() == 2

    # search shows the related customer label
    resp = client.get(f"/u/forms/{of_id}?q=ORD")
    _ok(resp)
    assert "Acme" in resp.get_data(as_text=True)

    # edit reduces the m:n links to one
    _ok(client.post(f"/u/forms/{of_id}/{oid}/edit",
                    data={"code": "ORD-1B", "customer_id": str(cid),
                          f"rel_{mn_id}": [str(tag_ids[0])]}, follow_redirects=True))
    with app.app_context():
        eng = get_engine()
        assert eng.connect().execute(text("SELECT COUNT(*) FROM j_order_tag")).scalar() == 1

    # clone then delete
    _ok(client.post(f"/u/forms/{of_id}/{oid}/clone",
                    data={"code": "ORD-2", "customer_id": str(cid)}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{of_id}/{oid}/delete", follow_redirects=True))
    with app.app_context():
        eng = get_engine()
        assert eng.connect().execute(text("SELECT COUNT(*) FROM `order`")).scalar() == 1
