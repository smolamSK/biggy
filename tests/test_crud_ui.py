"""CRUD, lists, record pages, attachments and UI chrome. (Split from test_features.py.)"""
import io
import os
import re

from sqlalchemy import select, text

from app import data_service, file_store
from app.db import SessionLocal, get_engine
from app.metadata.models import (
    Attachment,
    AuditLog,
    MetaField,
    MetaForm,
    MetaFormField,
    MetaTable,
    SavedView,
)
from tests.helpers import (
    _add_field,
    _doc_with_files,
    _make_connection,
    _make_feed_orm,
    _make_form,
    _make_form_p,
    _make_table,
    _make_trigger,
    _make_workflow,
    _new_amy,
    _ok,
    _raw_token,
    _setup,
    _status_field_id,
)


def test_audit_and_soft_delete(app, client):
    _setup(client)
    tid = _make_table(client, app, "note", "Note", "body")
    fid = _make_form(client, app, "note_form", "Notes", tid)
    _ok(client.post(f"/designer/tables/{tid}/flags",
                    data=dict(track_audit="y", soft_delete="y"), follow_redirects=True))

    _ok(client.post(f"/u/forms/{fid}/new", data={"body": "hello"}, follow_redirects=True))
    with app.app_context():
        eng = get_engine()
        with eng.connect() as c:
            row = c.execute(text("SELECT id, created_by, created_at FROM note")).mappings().first()
        assert row["created_by"] is not None and row["created_at"] is not None
        pk = row["id"]
        n_create = SessionLocal().scalar(
            select(AuditLog).where(AuditLog.action == "create", AuditLog.row_pk == pk))
        assert n_create is not None

    # edit -> updated_* stamped + an update audit entry with a diff
    _ok(client.post(f"/u/forms/{fid}/{pk}/edit", data={"body": "changed"}, follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT updated_by FROM note WHERE id=:i"),
                             {"i": pk}).scalar() is not None
        upd = SessionLocal().scalar(
            select(AuditLog).where(AuditLog.action == "update", AuditLog.row_pk == pk))
        assert upd is not None and "body" in upd.changes

    # soft delete -> hidden from list, kept in DB, visible in Trash, then restore
    _ok(client.post(f"/u/forms/{fid}/{pk}/delete", follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM note")).scalar() == 1
            assert c.execute(text("SELECT deleted_at FROM note WHERE id=:i"),
                             {"i": pk}).scalar() is not None
    listing = client.get(f"/u/forms/{fid}")
    assert "hello" not in listing.get_data(as_text=True) and \
           "changed" not in listing.get_data(as_text=True)
    trash = client.get(f"/u/forms/{fid}/trash")
    _ok(trash)
    assert "changed" in trash.get_data(as_text=True)
    _ok(client.post(f"/u/forms/{fid}/{pk}/restore", follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT deleted_at FROM note WHERE id=:i"),
                             {"i": pk}).scalar() is None

def test_permissions_per_form_and_ownership(app, client):
    _setup(client)
    tid = _make_table(client, app, "note", "Note", "body")
    fid = _make_form(client, app, "note_form", "Notes", tid)
    # a non-designer user
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))

    # default (no permission row) = write: amy can add
    _ok(amy.get(f"/u/forms/{fid}"))
    _ok(amy.post(f"/u/forms/{fid}/new", data={"body": "amy-1"}, follow_redirects=True))

    # set read-only for the user role
    _ok(client.post("/designer/permissions",
                    data={f"access_{fid}": "read"}, follow_redirects=True))
    assert amy.get(f"/u/forms/{fid}").status_code == 200          # read ok
    assert amy.post(f"/u/forms/{fid}/new", data={"body": "x"}).status_code == 403

    # none -> no access at all
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "none"},
                    follow_redirects=True))
    assert amy.get(f"/u/forms/{fid}").status_code == 403
    # designer still has access
    _ok(client.get(f"/u/forms/{fid}"))

    # ownership: amy sees only her own rows
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "write"},
                    follow_redirects=True))
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(row_owned="y"),
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"body": "boss-row"}, follow_redirects=True))
    _ok(amy.post(f"/u/forms/{fid}/new", data={"body": "amy-row"}, follow_redirects=True))
    page = amy.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "amy-row" in page and "boss-row" not in page

def test_master_detail_related_lists(app, client):
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    cust_fid = _make_form(client, app, "customer_form", "Customers", cust_tid)
    _make_table(client, app, "order", "Order", "code")
    with app.app_context():
        order_tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "order")).id
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _make_form(client, app, "order_form", "Orders", order_tid)

    _ok(client.post(f"/u/forms/{cust_fid}/new", data={"name": "Acme"}, follow_redirects=True))
    with app.app_context():
        cid = get_engine().connect().execute(text("SELECT id FROM customer LIMIT 1")).scalar()

    page = client.get(f"/u/forms/{cust_fid}/{cid}/edit")
    _ok(page)
    html = page.get_data(as_text=True)
    assert "Order" in html                       # related panel header
    assert f"customer_id={cid}" in html          # Add-order link pre-fills the FK
    assert "next=" in html                        # returns to the parent after add

def test_view_page_shows_related_tabs(app, client):
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    cust_fid = _make_form(client, app, "customer_form", "Customers", cust_tid)
    _make_form_p(client, app, "customer_view", "Customer", cust_tid, "view")
    _ok(client.post(f"/designer/tables/{cust_tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    _make_table(client, app, "order", "Order", "code")
    with app.app_context():
        order_tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "order")).id
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _make_form(client, app, "order_form", "Orders", order_tid)

    _ok(client.post(f"/u/forms/{cust_fid}/new", data={"name": "Acme"}, follow_redirects=True))
    with app.app_context():
        cid = get_engine().connect().execute(text("SELECT id FROM customer LIMIT 1")).scalar()
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO `order` (id, code, customer_id) VALUES (10,'O-1',:cid)"),
                      {"cid": cid})

    page = client.get(f"/u/view/{cust_tid}/{cid}")
    _ok(page)
    html = page.get_data(as_text=True)
    assert "tabs-wrap" in html and 'data-tab="tab-rel-1"' in html  # tabs rendered on the view page
    assert "Order (1)" in html                                     # related tab labelled with child + count
    assert "O-1" in html                                           # the child row is listed
    assert f"/u/view/{cust_tid}/{cid}" in html                     # child actions return to the view page
    assert 'data-tab="tab-history"' in html and "History (" in html  # audit history tab on the view page

def test_delete_impact_and_soft_dissociation(app, client):
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    cust_fid = _make_form(client, app, "customer_form", "Customers", cust_tid)
    order_tid = _make_table(client, app, "order", "Order", "code")
    _make_form(client, app, "order_form", "Orders", order_tid)
    tag_tid = _make_table(client, app, "tag", "Tag", "name")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Tags", from_table_id=cust_tid, to_table_id=tag_tid),
                    follow_redirects=True))
    _ok(client.post(f"/designer/tables/{cust_tid}/flags", data=dict(soft_delete="y"),
                    follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO customer (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO tag (id, name) VALUES (1,'vip')"))
            c.execute(text("INSERT INTO `order` (id, code, customer_id) VALUES (10,'O-1',1)"))
            c.execute(text("INSERT INTO j_customer_tag (customer_id, tag_id) VALUES (1,1)"))

    page = client.get(f"/u/forms/{cust_fid}/1/confirm-delete")
    _ok(page)
    html = page.get_data(as_text=True)
    assert "Order" in html and "Tags" in html and "Trash" in html
    assert "O-1" in html and "vip" in html      # the actual affected records, not just counts

    _ok(client.post(f"/u/forms/{cust_fid}/1/delete", follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT deleted_at FROM customer WHERE id=1")).scalar() is not None
            assert c.execute(text("SELECT customer_id FROM `order` WHERE id=10")).scalar() is None
            assert c.execute(text("SELECT COUNT(*) FROM j_customer_tag")).scalar() == 0

def test_soft_delete_removes_required_children(app, client):
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    cust_fid = _make_form(client, app, "customer_form", "Customers", cust_tid)
    order_tid = _make_table(client, app, "order", "Order", "code")
    # required FK (nullable off, on_delete CASCADE) -> NOT NULL column -> can't be nulled
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="CASCADE"),
                    follow_redirects=True))
    _ok(client.post(f"/designer/tables/{cust_tid}/flags", data=dict(soft_delete="y"),
                    follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO customer (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO `order` (id, code, customer_id) VALUES (10,'O-1',1)"))
    _ok(client.post(f"/u/forms/{cust_fid}/1/delete", follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM `order`")).scalar() == 0
            assert c.execute(text("SELECT deleted_at FROM customer WHERE id=1")).scalar() is not None

def test_hard_delete_preview_blocked_by_restrict(app, client):
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    cust_fid = _make_form(client, app, "customer_form", "Customers", cust_tid)
    order_tid = _make_table(client, app, "order", "Order", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="RESTRICT"),
                    follow_redirects=True))  # customer not soft-deleted -> hard delete path
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO customer (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO `order` (id, code, customer_id) VALUES (10,'O-1',1)"))
    page = client.get(f"/u/forms/{cust_fid}/1/confirm-delete")
    _ok(page)
    assert "Cannot delete" in page.get_data(as_text=True)

def test_dependent_dropdown(app, client):
    _setup(client)
    comp_tid = _make_table(client, app, "company", "Company", "name")
    _make_form(client, app, "company_form", "Companies", comp_tid)
    cont_tid = _make_table(client, app, "contact", "Contact", "name")
    _make_form(client, app, "contact_form", "Contacts", cont_tid)
    deal_tid = _make_table(client, app, "deal", "Deal", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Contact company", from_table_id=cont_tid, to_table_id=comp_tid,
                              field_name="company_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Deal company", from_table_id=deal_tid, to_table_id=comp_tid,
                              field_name="company_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Deal contact", from_table_id=deal_tid, to_table_id=cont_tid,
                              field_name="contact_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)

    with app.app_context():
        s = SessionLocal()
        deal = s.scalar(select(MetaTable).where(MetaTable.phys_name == "deal"))
        dform = s.scalar(select(MetaForm).where(MetaForm.name == "deal_form"))
        fields = {f.phys_name: f.id for f in deal.fields}
        items = {i.field_id: i.id for i in dform.items if i.kind == "field"}
        company_field_id = fields["company_id"]
        contact_item_id = items[fields["contact_id"]]

    # configure: filter Contact by Company (Match on = auto)
    _ok(client.post(f"/designer/forms/{deal_fid}/items/{contact_item_id}/edit",
                    data={"parent_field_id": company_field_id, "filter_field_id": 0},
                    follow_redirects=True))

    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO company (id, name) VALUES (1,'Acme'),(2,'Globex')"))
            c.execute(text("INSERT INTO contact (id, name, company_id) VALUES "
                           "(10,'Ann',1),(11,'Bob',2)"))

    page = client.get(f"/u/forms/{deal_fid}/new")
    _ok(page)
    html = page.get_data(as_text=True)
    assert 'data-parent-field="company_id"' in html          # contact select is dependent
    assert 'data-parent="1"' in html and 'data-parent="2"' in html   # each contact's company id

    # the dependency config survives a schema export/import (ids remapped)
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        dform2 = s.scalar(select(MetaForm).where(MetaForm.name == "deal_form"))
        ci = next(i for i in dform2.items if i.kind == "field"
                  and s.get(MetaField, i.field_id).phys_name == "contact_id")
        assert ci.parent_field_id is not None
        deal_fid2 = dform2.id
    assert 'data-parent-field="company_id"' in client.get(
        f"/u/forms/{deal_fid2}/new").get_data(as_text=True)

def test_record_view_and_links(app, client):
    _setup(client)
    comp_tid = _make_table(client, app, "company", "Company", "name")
    cont_tid = _make_table(client, app, "contact", "Contact", "name")
    deal_tid = _make_table(client, app, "deal", "Deal", "code")
    for frm, to, col in [(cont_tid, comp_tid, "company_id"),
                         (deal_tid, comp_tid, "company_id"),
                         (deal_tid, cont_tid, "contact_id")]:
        _ok(client.post("/designer/relations/new-m1",
                        data=dict(name=col, from_table_id=frm, to_table_id=to, field_name=col,
                                  on_delete="SET NULL", nullable="y"), follow_redirects=True))
    _make_form_p(client, app, "company_view", "Company", comp_tid, "view")
    _make_form_p(client, app, "contact_view", "Contact", cont_tid, "view")
    _make_form_p(client, app, "deal_view", "Deal", deal_tid, "view")
    contact_fid = _make_form_p(client, app, "contact_form", "Contacts", cont_tid, "data")
    deal_fid = _make_form_p(client, app, "deal_form", "Deals", deal_tid, "data")

    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO company (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO contact (id, name, company_id) VALUES (10,'Ann',1)"))
            c.execute(text("INSERT INTO deal (id, code, company_id, contact_id) "
                           "VALUES (100,'D-1',1,10)"))

    # contact view page links to the company view
    v = client.get(f"/u/view/{cont_tid}/10")
    _ok(v)
    assert "Ann" in v.get_data(as_text=True)
    assert f"/u/view/{comp_tid}/1" in v.get_data(as_text=True)

    # deal list: M:1 cells link to the referenced view + a View action to the deal view
    lh = client.get(f"/u/forms/{deal_fid}").get_data(as_text=True)
    assert f"/u/view/{comp_tid}/1" in lh and f"/u/view/{cont_tid}/10" in lh
    assert f"/u/view/{deal_tid}/100" in lh

    # a table with no view form is not viewable
    secret_tid = _make_table(client, app, "secret", "Secret", "name")
    assert client.get(f"/u/view/{secret_tid}/1").status_code == 404

    # Trash: soft-delete a contact, then open it from Trash
    _ok(client.post(f"/designer/tables/{cont_tid}/flags", data=dict(soft_delete="y"),
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{contact_fid}/10/delete", follow_redirects=True))
    trash = client.get(f"/u/forms/{contact_fid}/trash").get_data(as_text=True)
    assert f"/u/view/{cont_tid}/10" in trash          # View link in Trash
    _ok(client.get(f"/u/view/{cont_tid}/10"))          # deleted record still viewable

    # purpose survives schema export/import
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        assert SessionLocal().scalar(
            select(MetaForm).where(MetaForm.name == "company_view")).purpose == "view"

def test_list_export_csv_roundtrip(app, client):
    _setup(client)
    comp_tid = _make_table(client, app, "company", "Company", "name")
    wid_tid = _make_table(client, app, "widget", "Widget", "name")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="company_id", from_table_id=wid_tid, to_table_id=comp_tid,
                              field_name="company_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    comp_fid = _make_form(client, app, "company_form", "Companies", comp_tid)
    wid_fid = _make_form(client, app, "widget_form", "Widgets", wid_tid)
    _ok(client.post(f"/u/forms/{comp_fid}/new", data={"name": "Acme"}, follow_redirects=True))
    with app.app_context():
        cid = get_engine().connect().execute(text("SELECT id FROM company LIMIT 1")).scalar()
    for w in ("Sprocket", "Cog"):
        _ok(client.post(f"/u/forms/{wid_fid}/new", data={"name": w, "company_id": cid},
                        follow_redirects=True))

    resp = client.get(f"/u/forms/{wid_fid}/export.csv")
    _ok(resp)
    assert resp.mimetype == "text/csv"
    body = resp.get_data(as_text=True)
    assert "Sprocket" in body and "Cog" in body
    assert "Acme" in body                       # M:1 exported as its label, not the id

    flt = client.get(f"/u/forms/{wid_fid}/export.csv?q=Cog").get_data(as_text=True)
    assert "Cog" in flt and "Sprocket" not in flt

    # re-import the exported file (insert): "Acme" label resolves back to the FK
    res = client.post(f"/u/import/{wid_tid}",
                      data={"file": (io.BytesIO(resp.get_data()), "w.csv")},
                      content_type="multipart/form-data", follow_redirects=True)
    _ok(res)
    with app.app_context():
        assert get_engine().connect().execute(text("SELECT COUNT(*) FROM widget")).scalar() == 4

def test_bulk_delete_and_export(app, client):
    _setup(client)
    tid = _make_table(client, app, "item", "Item", "name")
    fid = _make_form(client, app, "item_form", "Items", tid)
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(soft_delete="y"),
                    follow_redirects=True))
    for n in ("alpha", "bravo", "charlie"):
        _ok(client.post(f"/u/forms/{fid}/new", data={"name": n}, follow_redirects=True))
    with app.app_context():
        ids = [r[0] for r in get_engine().connect()
               .execute(text("SELECT id FROM item ORDER BY id")).all()]

    conf = client.post(f"/u/forms/{fid}/bulk/delete", data={"ids": [ids[0], ids[1]]})
    _ok(conf)
    assert "Delete" in conf.get_data(as_text=True)
    _ok(client.post(f"/u/forms/{fid}/bulk/delete/confirm",
                    data={"ids": [ids[0], ids[1]]}, follow_redirects=True))
    with app.app_context():
        live = get_engine().connect().execute(
            text("SELECT COUNT(*) FROM item WHERE deleted_at IS NULL")).scalar()
    assert live == 1
    assert "alpha" in client.get(f"/u/forms/{fid}/trash").get_data(as_text=True)

    sel = client.post(f"/u/forms/{fid}/bulk/export.csv", data={"ids": [ids[2]]})
    _ok(sel)
    out = sel.get_data(as_text=True)
    assert "charlie" in out and "alpha" not in out and "bravo" not in out

def test_saved_views(app, client):
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    fid = _make_form(client, app, "lead_form", "Leads", tid)
    _ok(client.post(f"/u/forms/{fid}/views",
                    data={"name": "Hot", "query": "q=acme&sort=name"}, follow_redirects=True))
    assert "Hot" in client.get(f"/u/forms/{fid}").get_data(as_text=True)
    with app.app_context():
        vid = SessionLocal().scalar(select(SavedView)).id
    r = client.get(f"/u/forms/{fid}/views/{vid}")
    assert r.status_code == 302 and "q=acme" in r.headers["Location"]

    amy = _new_amy(app, client)
    assert "Hot" not in amy.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert amy.get(f"/u/forms/{fid}/views/{vid}").status_code == 404

    _ok(client.post(f"/u/forms/{fid}/views/{vid}/delete", follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(SavedView)) is None

def test_global_search(app, client):
    _setup(client)
    comp_tid = _make_table(client, app, "company", "Company", "name")
    _add_field(client, comp_tid, "notes", "text")
    _add_field(client, comp_tid, "employees", "integer")
    wid_tid = _make_table(client, app, "widget", "Widget", "name")
    _make_form_p(client, app, "company_view", "Company", comp_tid, "view")  # viewable
    comp_fid = _make_form(client, app, "company_form", "Companies", comp_tid)
    wid_fid = _make_form(client, app, "widget_form", "Widgets", wid_tid)    # no view form
    _ok(client.post(f"/u/forms/{comp_fid}/new",
                    data={"name": "Acme Corp", "notes": "leading zeppelin manufacturer",
                          "employees": "77145"}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{wid_fid}/new", data={"name": "Acme Gadget"}, follow_redirects=True))

    res = client.get("/u/search?q=Acme").get_data(as_text=True)
    assert "Acme Corp" in res
    assert "Acme Gadget" not in res                  # no view form -> not searchable
    assert f"/u/view/{comp_tid}/" in res

    # cross-column: a term living only in a non-display text column is found, with
    # the matched field named + highlighted, and a working "view all" link
    res = client.get("/u/search?q=zeppelin").get_data(as_text=True)
    assert "Acme Corp" in res and "Notes" in res
    assert "<mark>zeppelin</mark>" in res
    assert f"/u/forms/{comp_fid}?q=zeppelin" in res
    # numeric columns are not text-searched
    assert "Acme Corp" not in client.get("/u/search?q=77145").get_data(as_text=True)

    # the per-list search box also matches across text columns now
    lst = client.get(f"/u/forms/{comp_fid}?q=zeppelin").get_data(as_text=True)
    assert "Acme Corp" in lst
    assert "Acme Corp" not in client.get(
        f"/u/forms/{comp_fid}?q=dirigible").get_data(as_text=True)

    # the data-layer OR-group: any-of semantics in one filter entry
    with app.app_context():
        rows, total = data_service.list_rows(get_engine(), "company", filters=[
            {"any": [{"col": "name", "op": "contains", "value": "no-such"},
                     {"col": "notes", "op": "contains", "value": "zeppelin"}]}])
        assert total == 1 and rows[0]["name"] == "Acme Corp"

def test_csv_upsert(app, client):
    from app import importer
    _setup(client)
    tid = _make_table(client, app, "person", "Person", "name")
    _add_field(client, tid, "email", "string", is_unique="y", length="120")
    fid = _make_form(client, app, "person_form", "People", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "Ann", "email": "ann@x.com"},
                    follow_redirects=True))

    csv_text = "name,email\nAnnie,ann@x.com\nBob,bob@x.com\n"
    res = client.post(f"/u/import/{tid}",
                      data={"file": (io.BytesIO(csv_text.encode()), "p.csv"),
                            "mode": "upsert", "key_column": "email"},
                      content_type="multipart/form-data")
    _ok(res)
    with app.app_context():
        eng = get_engine()
        names = {r[0] for r in eng.connect()
                 .execute(text("SELECT name FROM person ORDER BY id")).all()}
        cnt = eng.connect().execute(text("SELECT COUNT(*) FROM person")).scalar()
    assert cnt == 2 and names == {"Annie", "Bob"}     # Ann updated in place, Bob inserted

    # ambiguous key -> per-row error (call importer directly on a non-unique column)
    with app.app_context():
        eng = get_engine()
        with eng.begin() as c:
            c.execute(text("INSERT INTO person (name, email) VALUES "
                           "('Dup','d1@x.com'),('Dup','d2@x.com')"))
        session = SessionLocal()
        table = session.get(MetaTable, tid)
        result = importer.import_rows(session, eng, table, "name,email\nDup,new@x.com\n",
                                      skip_invalid=True, mode="upsert", key_field="name")
    assert result["errors"] and result["updated"] == 0

def test_inline_cell_edit(app, client):
    _setup(client)
    tid = _make_table(client, app, "task", "Task", "title")       # title = display field
    _add_field(client, tid, "priority", "integer", max_value="10")
    _add_field(client, tid, "status", "enum", enum_options="open\nclosed")
    _add_field(client, tid, "done", "boolean")
    fid = _make_form(client, app, "task_form", "Tasks", tid)
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T1", "priority": "1", "status": "open"}, follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM task LIMIT 1")).scalar()
    url = f"/u/forms/{fid}/{pk}/cell"

    r = client.post(url, data={"col": "priority", "value": "5"})
    _ok(r)
    assert r.get_json()["ok"] is True
    _ok(client.post(url, data={"col": "done", "value": "1"}))

    assert client.post(url, data={"col": "status", "value": "nope"}).status_code == 400  # bad enum
    assert client.post(url, data={"col": "priority", "value": "99"}).status_code == 400  # > max
    assert client.post(url, data={"col": "title", "value": "x"}).status_code == 400      # display col

    with app.app_context():
        with get_engine().connect() as c:
            row = c.execute(text("SELECT priority, done FROM task WHERE id=:i"),
                            {"i": pk}).mappings().first()
    assert row["priority"] == 5 and row["done"] in (1, True)

    # read-only user cannot inline-edit
    amy = _new_amy(app, client)
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "read"},
                    follow_redirects=True))
    assert amy.post(url, data={"col": "priority", "value": "3"}).status_code == 403

def test_edit_page_tabs(app, client):
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    cust_fid = _make_form(client, app, "customer_form", "Customers", cust_tid)
    _make_table(client, app, "order", "Order", "code")
    with app.app_context():
        order_tid = SessionLocal().scalar(
            select(MetaTable).where(MetaTable.phys_name == "order")).id
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Order customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    _make_form(client, app, "order_form", "Orders", order_tid)
    _ok(client.post(f"/designer/tables/{cust_tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))

    _ok(client.post(f"/u/forms/{cust_fid}/new", data={"name": "Acme"}, follow_redirects=True))
    with app.app_context():
        cid = get_engine().connect().execute(text("SELECT id FROM customer LIMIT 1")).scalar()
    _ok(client.post(f"/u/forms/{cust_fid}/{cid}/edit", data={"name": "Acme Inc"},
                    follow_redirects=True))  # creates audit history

    html = client.get(f"/u/forms/{cust_fid}/{cid}/edit").get_data(as_text=True)
    assert "data-tabs" in html
    assert 'data-tab="tab-details"' in html          # Details tab
    assert 'id="tab-rel-1"' in html                  # a related-list tab panel
    assert "Order" in html                           # related tab label
    assert 'id="tab-history"' in html                # History tab (audit enabled)
    assert f"customer_id={cid}" in html              # related Add-link still in the DOM

    # new record: no related lists / history -> plain form, no tab chrome
    assert "data-tabs" not in client.get(f"/u/forms/{cust_fid}/new").get_data(as_text=True)

def test_attachments_crud(app, client):
    _setup(client)
    tid, fid, fids = _doc_with_files(client, app)

    # file/image are virtual: no physical columns, but the MetaFields exist
    with app.app_context():
        cols = data_service.column_names(data_service.reflect_table(get_engine(), "doc"))
    assert "title" in cols and "photo" not in cols and "attach" not in cols

    # create a record with two images for the photo field
    res = client.post(f"/u/forms/{fid}/new", data={
        "title": "Doc1",
        f"file_{fids['photo']}": [(io.BytesIO(b"img-a"), "a.png"),
                                  (io.BytesIO(b"img-b"), "b.png")],
    }, content_type="multipart/form-data", follow_redirects=True)
    _ok(res)
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM doc LIMIT 1")).scalar()
        atts = SessionLocal().scalars(select(Attachment).where(
            Attachment.field_id == fids["photo"], Attachment.row_pk == pk)
            .order_by(Attachment.id)).all()
        assert len(atts) == 2
        first_id, first_stored = atts[0].id, atts[0].stored_name

    # serve returns the bytes with an image content-type
    r = client.get(f"/u/attachment/{first_id}")
    _ok(r)
    assert r.mimetype.startswith("image/") and r.get_data() == b"img-a"

    # remove one on save -> row + file gone, the other remains
    _ok(client.post(f"/u/forms/{fid}/{pk}/edit", data={
        "title": "Doc1", f"rm_{fids['photo']}": first_id,
    }, content_type="multipart/form-data", follow_redirects=True))
    with app.app_context():
        left = SessionLocal().scalars(select(Attachment).where(
            Attachment.field_id == fids["photo"], Attachment.row_pk == pk)).all()
        assert len(left) == 1 and left[0].id != first_id
        assert not os.path.exists(file_store.abs_path(fids["photo"], first_stored))

    # a .txt rejected for an image field (record still saves, no attachment)
    _ok(client.post(f"/u/forms/{fid}/new", data={
        "title": "Doc2", f"file_{fids['photo']}": (io.BytesIO(b"x"), "bad.txt"),
    }, content_type="multipart/form-data", follow_redirects=True))
    with app.app_context():
        total = SessionLocal().scalars(
            select(Attachment).where(Attachment.field_id == fids["photo"])).all()
    assert len(total) == 1  # unchanged

    # deleting the field removes its attachments, no DDL error (no column existed)
    _ok(client.post(f"/designer/tables/{tid}/fields/{fids['photo']}/delete",
                    follow_redirects=True))
    with app.app_context():
        gone = SessionLocal().scalars(
            select(Attachment).where(Attachment.field_id == fids["photo"])).all()
    assert gone == []

def test_attachment_access(app, client):
    _setup(client)
    _tid, fid, fids = _doc_with_files(client, app)
    _ok(client.post(f"/u/forms/{fid}/new", data={
        "title": "D", f"file_{fids['photo']}": (io.BytesIO(b"img"), "a.png"),
    }, content_type="multipart/form-data", follow_redirects=True))
    with app.app_context():
        att_id = SessionLocal().scalar(select(Attachment)).id

    _ok(client.get(f"/u/attachment/{att_id}"))          # designer can fetch
    amy = _new_amy(app, client)
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "none"},
                    follow_redirects=True))
    assert amy.get(f"/u/attachment/{att_id}").status_code == 403

def test_attachment_schema_roundtrip(app, client):
    _setup(client)
    _doc_with_files(client, app)
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"),
                          "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        types = {f.phys_name: f.data_type for f in SessionLocal().scalar(
            select(MetaTable).where(MetaTable.phys_name == "doc")).fields}
        cols = data_service.column_names(data_service.reflect_table(get_engine(), "doc"))
    assert types.get("photo") == "image" and types.get("attach") == "file"
    assert "photo" not in cols and "attach" not in cols

def test_theme_picker_present(app, client):
    _setup(client)
    html = client.get("/u/").get_data(as_text=True)
    assert 'id="theme-select"' in html       # header theme picker
    assert "biggy.theme" in html             # inline pre-paint bootstrap
    assert "theme.js" in html

def test_form_sections_subtabs(app, client):
    _setup(client)
    tid = _make_table(client, app, "acct", "Account", "name")
    _add_field(client, tid, "email", "string", length="120")
    _ok(client.post("/designer/forms/new",
                    data=dict(name="acct_form", title="Accounts", table_id=tid),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        fid = s.scalar(select(MetaForm).where(MetaForm.name == "acct_form")).id
        fids = {f.phys_name: f.id for f in
                s.scalar(select(MetaTable).where(MetaTable.phys_name == "acct")).fields}
    # build the form: a "Billing" section, then the two fields
    _ok(client.post(f"/designer/forms/{fid}",
                    data=dict(kind="section", label_override="Billing",
                              field_id=0, relation_id=0, position=0), follow_redirects=True))
    _ok(client.post(f"/designer/forms/{fid}",
                    data=dict(kind="field", field_id=fids["name"], relation_id=0, position=1),
                    follow_redirects=True))
    _ok(client.post(f"/designer/forms/{fid}",
                    data=dict(kind="field", field_id=fids["email"], relation_id=0, position=2),
                    follow_redirects=True))
    with app.app_context():
        sec = SessionLocal().scalar(select(MetaFormField).where(
            MetaFormField.form_id == fid, MetaFormField.kind == "section"))
        assert sec is not None and sec.label_override == "Billing"

    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "A", "email": "a@x.com"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:  # close before the replace-import drops 'acct'
            pk = c.execute(text("SELECT id FROM acct LIMIT 1")).scalar()
    html = client.get(f"/u/forms/{fid}/{pk}/edit").get_data(as_text=True)
    assert "data-tabs" in html and 'data-tab="sec-1"' in html and "Billing" in html

    # the section item survives a schema export/import round-trip
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"),
                          "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        assert SessionLocal().scalar(
            select(MetaFormField).where(MetaFormField.kind == "section")) is not None

def test_keyboard_shortcut_hooks(app, client):
    _setup(client)
    tid = _make_table(client, app, "thing", "Thing", "name")
    fid = _make_form(client, app, "thing_form", "Things", tid)
    html = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "shortcuts.js" in html
    assert 'data-sc="new"' in html           # 'n' shortcut target
    assert 'type="search"' in html           # '/' focuses the global search box

def test_designer_nav_grouped(app, client):
    _setup(client)
    html = client.get("/designer/").get_data(as_text=True)
    for label in ("Model", "Interface", "Data", "Admin"):
        assert label in html
    assert 'data-menu-id="dz-model"' in html
    assert "designer.relations" not in html        # rendered as resolved URLs, not endpoints
    assert "/designer/relations" in html

def test_kanban_board_and_move(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nreview\napproved")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    sid = _status_field_id(app)
    _make_workflow(client, app, sid,
                   [{"from": "draft", "to": "review", "roles": []},
                    {"from": "review", "to": "approved", "roles": []}], "draft")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO ticket (title, status) VALUES "
                           "('A','draft'),('B','review'),('C','draft')"))
        with get_engine().connect() as cc:
            pk = cc.execute(text("SELECT id FROM ticket WHERE title='A'")).scalar()

    html = client.get(f"/u/forms/{fid}/kanban").get_data(as_text=True)
    for s in ("draft", "review", "approved"):
        assert s in html
    assert "A" in html and 'data-col="status"' in html
    assert "status=review" in html                 # "+add" link on the review column

    # the Kanban drop path is the cell endpoint → workflow-enforced
    cell = f"/u/forms/{fid}/{pk}/cell"
    assert client.post(cell, data={"col": "status", "value": "approved"}).status_code == 409
    _ok(client.post(cell, data={"col": "status", "value": "review"}))

    assert f"/u/forms/{fid}/kanban" in client.get(f"/u/forms/{fid}").get_data(as_text=True)

def test_calendar_view(app, client):
    _setup(client)
    tid = _make_table(client, app, "event", "Event", "title")
    _add_field(client, tid, "due", "date")
    fid = _make_form(client, app, "event_form", "Events", tid)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO event (title, due) VALUES "
                           "('MayEvt','2024-05-10'),('JunEvt','2024-06-02')"))

    html = client.get(f"/u/forms/{fid}/calendar?date=due&month=2024-05").get_data(as_text=True)
    assert "MayEvt" in html and "JunEvt" not in html        # only the in-month record
    assert "due=2024-05-10" in html                          # add-on-day link
    assert "2024-06" in html and "2024-04" in html           # prev/next-month nav

    assert f"/u/forms/{fid}/calendar" in client.get(f"/u/forms/{fid}").get_data(as_text=True)

def test_clickable_designer_refs(app, client):
    """Designer screens link object references to the object's editor.

    The designer sidebar already links every table, so table-editor links are
    asserted by *count* (sidebar + content); links the sidebar never contains
    (relation/form/connection editors) are asserted by presence.
    """
    from app.metadata.models import MetaRelation
    _setup(client)
    cust = _make_table(client, app, "customer", "Customer", "name")
    order = _make_table(client, app, "ordr", "Order", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="ordcust", from_table_id=order, to_table_id=cust,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    ofid = _make_form(client, app, "order_form", "Orders", order)
    with app.app_context():
        rid = SessionLocal().scalar(select(MetaRelation).where(MetaRelation.kind == "m1")).id

    def html(path):
        return client.get(path).get_data(as_text=True)

    # Relations: from/to tables link to their editors (sidebar has 1 each → ≥2)
    rels = html("/designer/relations")
    assert rels.count(f'/designer/tables/{cust}"') >= 2
    assert rels.count(f'/designer/tables/{order}"') >= 2

    # Forms: the Table column links to the table editor
    assert html("/designer/forms").count(f'/designer/tables/{order}"') >= 2

    # Table view: relation field → related table; relations list → relation editor
    tv = html(f"/designer/tables/{order}")
    assert tv.count(f'/designer/tables/{cust}"') >= 2
    assert f"/designer/relations/{rid}/edit" in tv

    # Permissions: form title → form editor
    assert f'/designer/forms/{ofid}"' in html("/designer/permissions")

    # Menus: a form item → form editor, a list item → table editor
    _ok(client.post("/designer/menus/new", data={"label": "Orders", "kind": "form",
                    "parent_id": 0, "target_form_id": ofid, "target_table_id": 0,
                    "position": 0}, follow_redirects=True))
    _ok(client.post("/designer/menus/new", data={"label": "Order list", "kind": "list",
                    "parent_id": 0, "target_form_id": 0, "target_table_id": order,
                    "position": 1}, follow_redirects=True))
    menus = html("/designer/menus")
    assert f'/designer/forms/{ofid}"' in menus and menus.count(f'/designer/tables/{order}"') >= 2

    # Triggers + Workflows: their table links to the table editor
    _make_trigger(app, "ordr", event="create", in_app=True, notify_target="actor")
    assert html("/designer/triggers").count(f'/designer/tables/{order}"') >= 2
    _add_field(client, order, "status", "enum", enum_options="new\ndone")
    _make_workflow(client, app, _status_field_id(app, "ordr"),
                   [{"from": "new", "to": "done", "roles": []}], "new")
    assert html("/designer/workflows").count(f'/designer/tables/{order}"') >= 2

    # Feeds: source table → table editor, connection → connection editor
    cid = _make_connection(app, client, _raw_token(app))
    _make_feed_orm(app, order, cid, "remote_t", [("code", "name")], event="create")
    fd = html("/designer/feeds")
    assert fd.count(f'/designer/tables/{order}"') >= 2 and f'/designer/connections/{cid}"' in fd

def test_audit_log_links_to_record(app, client):
    _setup(client)
    tid = _make_table(client, app, "doc", "Doc", "title")
    fid = _make_form(client, app, "doc_form", "Docs", tid)
    _make_form_p(client, app, "doc_view", "Doc", tid, "view")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "D1"}, follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM doc LIMIT 1")).scalar()

    audit = client.get("/designer/audit").get_data(as_text=True)
    assert f"/u/view/{tid}/{pk}" in audit            # the row links to the record
    assert audit.count(f'/designer/tables/{tid}"') >= 2   # the table links to its editor

def test_notification_links_to_record(app, client):
    _setup(client)
    tid = _make_table(client, app, "doc", "Doc", "title")
    fid = _make_form(client, app, "doc_form", "Docs", tid)
    _make_form_p(client, app, "doc_view", "Doc", tid, "view")
    _make_trigger(app, "doc", event="create", in_app=True, notify_target="actor",
                  message="New {title}")
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "D1"}, follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM doc LIMIT 1")).scalar()

    assert f"/u/view/{tid}/{pk}" in client.get("/u/notifications").get_data(as_text=True)


def test_merge_duplicates(app, client):
    """The merge tool repoints FKs, moves mn links, fills blanks, deletes the dup."""
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    _add_field(client, cust_tid, "email", "email")
    _ok(client.post(f"/designer/tables/{cust_tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    order_tid = _make_table(client, app, "ordr", "Order", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    tag_tid = _make_table(client, app, "tag", "Tag", "name")
    _ok(client.post("/designer/relations/new-mn",
                    data=dict(name="Tags", from_table_id=cust_tid, to_table_id=tag_tid),
                    follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO customer (id, name, email) VALUES (1, 'Acme', NULL)"))
            c.execute(text("INSERT INTO customer (id, name, email) "
                           "VALUES (2, 'acme ', 'sales@acme.io')"))
            c.execute(text("INSERT INTO tag (id, name) VALUES (1, 'vip'), (2, 'eu')"))
            c.execute(text("INSERT INTO ordr (id, code, customer_id) VALUES "
                           "(10, 'O-1', 2), (11, 'O-2', 1)"))
            c.execute(text("INSERT INTO j_customer_tag (customer_id, tag_id) VALUES "
                           "(1, 1), (2, 1), (2, 2)"))   # tag 1 on both, tag 2 only on the dup

    # preview names what will happen
    prev = client.get(f"/designer/reconcile?table_id={cust_tid}&survivor=1&duplicate=2"
                      ).get_data(as_text=True)
    assert "sales@acme.io" in prev and "Order (1)" in prev and "Merge #2 into #1" in prev

    _ok(client.post("/designer/reconcile",
                    data={"table_id": cust_tid, "survivor": "1", "duplicate": "2"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT customer_id FROM ordr WHERE id=10")).scalar() == 1
            assert c.execute(text("SELECT email FROM customer WHERE id=1")).scalar() \
                == "sales@acme.io"                              # blank filled from the dup
            tags = {r[0] for r in c.execute(
                text("SELECT tag_id FROM j_customer_tag WHERE customer_id=1")).all()}
            assert tags == {1, 2}                               # union, no duplicates
            assert c.execute(text("SELECT COUNT(*) FROM j_customer_tag WHERE customer_id=2")
                             ).scalar() == 0
            assert c.execute(text("SELECT COUNT(*) FROM customer WHERE id=2")).scalar() == 0
        s = SessionLocal()
        actions = [a.action for a in s.scalars(select(AuditLog).where(
            AuditLog.table_phys == "customer"))]
        assert "update" in actions and "delete" in actions      # the merge is audit-logged

    # merging a record into itself is refused
    r = client.post("/designer/reconcile",
                    data={"table_id": cust_tid, "survivor": "1", "duplicate": "1"},
                    follow_redirects=True)
    assert "same record" in r.get_data(as_text=True)


def test_service_catalog_and_my_requests(app, client):
    """Catalog cards from flagged forms; my-requests shows only the caller's records."""
    _setup(client)
    tid = _make_table(client, app, "it_request", "IT request", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\ndone")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(row_owned="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "it_req_form", "New IT request", tid)
    other_tid = _make_table(client, app, "misc", "Misc", "name")
    _make_form(client, app, "misc_form", "Misc", other_tid)      # NOT flagged

    # not yet in the catalog
    assert "New IT request" not in client.get("/u/catalog").get_data(as_text=True)
    _ok(client.post(f"/designer/forms/{fid}/catalog",
                    data={"in_catalog": "y", "catalog_group": "IT"}, follow_redirects=True))
    cat = client.get("/u/catalog").get_data(as_text=True)
    assert "New IT request" in cat and "IT" in cat and f"/u/forms/{fid}/new" in cat
    assert "Misc" not in cat                                   # unflagged form absent

    # amy submits a request; each user sees only their own under My requests
    _new_amy(app, client)
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    _ok(amy.post(f"/u/forms/{fid}/new", data={"title": "Laptop please", "status": "new"},
                 follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "Boss request", "status": "new"},
                    follow_redirects=True))
    mine = amy.get("/u/my-requests").get_data(as_text=True)
    assert "Laptop please" in mine and "Boss request" not in mine
    boss = client.get("/u/my-requests").get_data(as_text=True)
    assert "Boss request" in boss and "Laptop please" not in boss

    # the catalog flags round-trip through schema export/import
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        mf = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == "it_req_form"))
        assert mf.in_catalog and mf.catalog_group == "IT"


def test_kb_example_searchable(app, client):
    """The knowledge-base example loads; articles render markdown + hit global search."""
    _setup(client)
    _ok(client.post("/designer/examples/kb/load", follow_redirects=True))
    res = client.get("/u/search?q=VPN").get_data(as_text=True)
    assert "VPN troubleshooting" in res
    with app.app_context():
        art = SessionLocal().scalar(select(MetaTable).where(
            MetaTable.phys_name == "kb_article"))
    vh = client.get(f"/u/view/{art.id}/2").get_data(as_text=True)
    assert "<h2>Common fixes</h2>" in vh                       # markdown rendered
    assert "<code>vpn.example.com</code>" in vh


def test_ui_polish(app, client):
    """Pickers, breadcrumbs, live-badge endpoint, unsaved guard, htmx removal."""
    _setup(client)
    cust_tid = _make_table(client, app, "customer", "Customer", "name")
    _make_form(client, app, "customer_form", "Customers", cust_tid)
    _make_form_p(client, app, "customer_view", "Customer", cust_tid, "view")
    order_tid = _make_table(client, app, "ordr", "Order", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="Customer", from_table_id=order_tid, to_table_id=cust_tid,
                              field_name="customer_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    order_fid = _make_form(client, app, "order_form", "Orders", order_tid)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO customer (id, name) VALUES (1, 'Acme')"))

    page = client.get(f"/u/forms/{order_fid}/new").get_data(as_text=True)
    assert 'data-picker' in page                       # relation select is enhanceable
    assert 'pickers.js' in page
    assert 'data-guard' in page                        # unsaved-changes guard armed
    assert 'class="crumbs"' in page                    # breadcrumb on the edit/new page

    vh = client.get(f"/u/view/{cust_tid}/1").get_data(as_text=True)
    assert 'class="crumbs"' in vh                      # breadcrumb back to the list
    assert "/u/forms/" in vh

    r = client.get("/u/badges")
    assert r.status_code == 200
    d = r.get_json()
    assert set(d) == {"notifications", "approvals"}    # live-badge JSON shape

    base = client.get("/u/").get_data(as_text=True)
    assert "htmx" not in base                          # dead dependency dropped
    assert 'id="badge-notif"' in base and 'id="nav-burger"' in base

    anon = app.test_client()
    assert anon.get("/u/badges").status_code == 302    # login required


def test_ui_modern(app, client):
    """SVG icons, account menu, list kebab/chips/aria-sort, palette, home page."""
    _setup(client)
    tid = _make_table(client, app, "gadget", "Gadget", "name")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "gadget_form", "Gadgets", tid)
    _make_form_p(client, app, "gadget_view", "Gadget", tid, "view")
    _ok(client.post("/designer/menus/new", data={"label": "Gadgets", "kind": "form",
                    "parent_id": 0, "target_form_id": fid, "target_table_id": 0,
                    "position": 0}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "Widget"}, follow_redirects=True))

    # topbar: SVG icons replace emoji; account dropdown; skip link; palette wired
    base = client.get("/u/").get_data(as_text=True)
    assert "<svg" in base and "\U0001f514" not in base       # no 🔔 emoji
    assert 'class="menu account-menu"' in base and "Sign out" in base
    assert 'class="skip-link"' in base and 'id="main"' in base
    assert "palette.js" in base

    # home: quick-access card for the menu item + the record just touched
    assert 'class="card qa-card"' in base
    assert "Recently updated" in base and "Widget" in base

    # sign-in: brand header; no palette for anonymous visitors
    anon = app.test_client()
    login_page = anon.get("/auth/login").get_data(as_text=True)
    assert 'class="auth-brand"' in login_page
    assert "palette.js" not in login_page

    # list page: aria-sort, row kebab, toolbar overflow menu
    lst = client.get(f"/u/forms/{fid}", query_string={"sort": "name"}).get_data(as_text=True)
    assert 'aria-sort="ascending"' in lst
    assert 'aria-label="Row actions"' in lst
    assert 'class="menu-panel"' in lst and "Export CSV" in lst

    # filter chips: one per active condition; removing one keeps the other
    lst = client.get(
        f"/u/forms/{fid}?fcol=name&fop=contains&fval=Wid&fcol=name&fop=ne&fval=zzz"
    ).get_data(as_text=True)
    assert lst.count('class="fchip"') == 2
    m = re.search(r'<span class="fchip">.*?href="([^"]+)"', lst, re.S)
    after = client.get(m.group(1).replace("&amp;", "&")).get_data(as_text=True)
    assert after.count('class="fchip"') == 1
    assert "Widget" in after                              # row still matches "ne zzz"

    # empty state with a filter active vs. without records
    none = client.get(f"/u/forms/{fid}?fcol=name&fop=eq&fval=nope").get_data(as_text=True)
    assert "No matching records" in none

    # shipped assets: the Inter font and the palette actually serve
    assert client.get("/static/fonts/InterVariable.woff2").status_code == 200
    assert "@font-face" in client.get("/static/app.css").get_data(as_text=True)
    assert client.get("/static/palette.js").status_code == 200
