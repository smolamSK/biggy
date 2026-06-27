"""Audit/soft-delete, access control, and master-detail (live test DB)."""
import hashlib
import hmac
import io
import json
import os
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app import data_service, file_store
from app.metadata.models import (
    AppUser,
    ApprovalAction,
    ApprovalRequest,
    ApprovalStep,
    Attachment,
    AuditLog,
    Connection,
    DataSource,
    MetaField,
    MetaForm,
    MetaFormField,
    MetaPermission,
    MetaTable,
    Notification,
    PullSource,
    RateHit,
    ReportDef,
    SavedView,
    SlaClock,
    SlaPolicy,
    Webhook,
    Workflow,
)


def _ok(resp):
    assert resp.status_code < 400, resp.get_data(as_text=True)[:300]


def _setup(client):
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))


def _make_table(client, app, phys, label, field):
    _ok(client.post("/designer/tables/new", data=dict(phys_name=phys, label=label),
                    follow_redirects=True))
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys)).id
    _ok(client.post(f"/designer/tables/{tid}/fields",
                    data=dict(phys_name=field, label=field.title(), data_type="string",
                              length=80, nullable="y"), follow_redirects=True))
    return tid


def _make_form(client, app, name, title, tid):
    _ok(client.post("/designer/forms/new",
                    data=dict(name=name, title=title, table_id=tid), follow_redirects=True))
    with app.app_context():
        f = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == name))
        fid, field_ids = f.id, {fd.phys_name: fd.id for fd in f.table.fields}
    for fphys, fid_ in field_ids.items():
        _ok(client.post(f"/designer/forms/{fid}", data=dict(kind="field", field_id=fid_),
                        follow_redirects=True))
    return fid


# --------------------------------------------------------------------------- #
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


def _make_form_p(client, app, name, title, tid, purpose="data"):
    _ok(client.post("/designer/forms/new",
                    data=dict(name=name, title=title, table_id=tid, purpose=purpose),
                    follow_redirects=True))
    with app.app_context():
        f = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == name))
        fid, field_ids = f.id, [fd.id for fd in f.table.fields]
    for fid_ in field_ids:
        _ok(client.post(f"/designer/forms/{fid}", data=dict(kind="field", field_id=fid_),
                        follow_redirects=True))
    return fid


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


def _topo_graph(client, table_id, pk, **params):
    """GET the topology page and return its embedded graph JSON."""
    import json as _json
    import re as _re
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    res = client.get(f"/u/topology/{table_id}/{pk}" + (f"?{qs}" if qs else ""))
    _ok(res)
    m = _re.search(r'id="topo-graph">(.*?)</script>', res.get_data(as_text=True), _re.S)
    return _json.loads(m.group(1)) if m else None


def test_topology_impact_map(app, client):
    _setup(client)
    site_tid = _make_table(client, app, "site", "Site", "name")
    rack_tid = _make_table(client, app, "rack", "Rack", "code")
    mach_tid = _make_table(client, app, "machine", "Machine", "name")
    nic_tid = _make_table(client, app, "nic", "Nic", "name")
    hidden_tid = _make_table(client, app, "hidden_ci", "Hidden", "name")
    # view forms make a table reachable in the map; hidden_ci deliberately gets none
    for name, tid in [("site", site_tid), ("rack", rack_tid),
                      ("machine", mach_tid), ("nic", nic_tid)]:
        _make_form_p(client, app, f"{name}_view", name.title(), tid, "view")
    # rack→site, machine→rack, nic→machine, hidden_ci→rack (hidden has NO view form)
    for nm, frm, to, col in [("rack_site", rack_tid, site_tid, "site_id"),
                             ("machine_rack", mach_tid, rack_tid, "rack_id"),
                             ("nic_machine", nic_tid, mach_tid, "machine_id"),
                             ("hidden_rack", hidden_tid, rack_tid, "rack_id")]:
        _ok(client.post("/designer/relations/new-m1",
                        data=dict(name=nm, from_table_id=frm, to_table_id=to, field_name=col,
                                  on_delete="SET NULL", nullable="y"), follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO site (id, name) VALUES (1,'DC1')"))
            c.execute(text("INSERT INTO rack (id, code, site_id) VALUES (1,'R1',1)"))
            c.execute(text("INSERT INTO machine (id, name, rack_id) VALUES (1,'M1',1)"))
            c.execute(text("INSERT INTO nic (id, name, machine_id) VALUES (1,'N1',1)"))
            c.execute(text("INSERT INTO hidden_ci (id, name, rack_id) VALUES (1,'SECRETX',1)"))

    # both directions, depth 2 from the rack: upstream site + downstream machine→nic
    g = _topo_graph(client, rack_tid, 1, direction="both", depth=2)
    labels = {n["label"] for n in g["nodes"]}
    assert {"R1", "M1", "N1", "DC1"} <= labels      # root + downstream chain + upstream parent
    assert "SECRETX" not in labels                  # child table without a view form is excluded
    assert g["truncated"] is False
    # an edge points from the machine to the rack it depends on
    assert any(e["directed"] and e["kind"] == "m1" for e in g["edges"])

    # direction filter: upstream only, depth 1 → just the parent site, no children
    g_up = _topo_graph(client, rack_tid, 1, direction="upstream", depth=1)
    up = {n["label"] for n in g_up["nodes"]}
    assert "DC1" in up and "M1" not in up

    # depth clamps to TOPOLOGY_MAX_DEPTH (no error on an over-large request)
    _ok(client.get(f"/u/topology/{rack_tid}/1?depth=99"))

    # node cap flips the truncated flag
    app.config["TOPOLOGY_MAX_NODES"] = 2
    try:
        g_cap = _topo_graph(client, rack_tid, 1, direction="both", depth=2)
        assert g_cap["truncated"] is True and len(g_cap["nodes"]) == 2
    finally:
        app.config["TOPOLOGY_MAX_NODES"] = 150

    # the view page links to the impact map
    assert f"/u/topology/{rack_tid}/1" in client.get(f"/u/view/{rack_tid}/1").get_data(as_text=True)


def _add_field(client, tid, phys, data_type, **kw):
    data = dict(phys_name=phys, label=phys.title(), data_type=data_type, nullable="y")
    data.update(kw)
    _ok(client.post(f"/designer/tables/{tid}/fields", data=data, follow_redirects=True))


def _new_amy(app, client):
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    return amy


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
    wid_tid = _make_table(client, app, "widget", "Widget", "name")
    _make_form_p(client, app, "company_view", "Company", comp_tid, "view")  # viewable
    comp_fid = _make_form(client, app, "company_form", "Companies", comp_tid)
    wid_fid = _make_form(client, app, "widget_form", "Widgets", wid_tid)    # no view form
    _ok(client.post(f"/u/forms/{comp_fid}/new", data={"name": "Acme Corp"}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{wid_fid}/new", data={"name": "Acme Gadget"}, follow_redirects=True))

    res = client.get("/u/search?q=Acme").get_data(as_text=True)
    assert "Acme Corp" in res
    assert "Acme Gadget" not in res                  # no view form -> not searchable
    assert f"/u/view/{comp_tid}/" in res


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


def _doc_with_files(client, app):
    """Build a 'doc' table with an image + file field and a form; return ids."""
    tid = _make_table(client, app, "doc", "Doc", "title")
    _add_field(client, tid, "photo", "image")
    _add_field(client, tid, "attach", "file")
    fid = _make_form(client, app, "doc_form", "Docs", tid)
    with app.app_context():
        fields = SessionLocal().scalar(
            select(MetaTable).where(MetaTable.phys_name == "doc")).fields
        fids = {f.phys_name: f.id for f in fields}
    return tid, fid, fids


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


def _csv_rows(text_body):
    import csv as _csv
    lines = [ln for ln in text_body.splitlines() if ln.strip()]
    return {row[0]: row for row in _csv.reader(lines)}


def test_report_aggregate_and_csv(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _add_field(client, tid, "amount", "integer")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO sale (name, tier, amount) VALUES "
                           "('a','gold',100),('b','gold',50),('c','silver',30)"))

    q = "group=tier&metric=count&metric=sum:amount"
    html = client.get(f"/u/report/{tid}?{q}")
    _ok(html)
    assert "gold" in html.get_data(as_text=True)

    csvr = client.get(f"/u/report/{tid}?{q}&export=csv")
    _ok(csvr)
    assert csvr.mimetype == "text/csv"
    rows = _csv_rows(csvr.get_data(as_text=True))
    assert rows["gold"][1:] == ["2", "150"]
    assert rows["silver"][1:] == ["1", "30"]
    assert rows["Total"][1:] == ["3", "180"]          # grand totals incl. across groups


def test_report_group_by_relation(app, client):
    _setup(client)
    comp_tid = _make_table(client, app, "company", "Company", "name")
    sale_tid = _make_table(client, app, "sale", "Sale", "code")
    _ok(client.post("/designer/relations/new-m1",
                    data=dict(name="company_id", from_table_id=sale_tid, to_table_id=comp_tid,
                              field_name="company_id", on_delete="SET NULL", nullable="y"),
                    follow_redirects=True))
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO company (id, name) VALUES (1,'Acme')"))
            c.execute(text("INSERT INTO sale (code, company_id) VALUES ('s1',1),('s2',1)"))
    body = client.get(f"/u/report/{sale_tid}?group=company_id&metric=count").get_data(as_text=True)
    assert "Acme" in body                              # grouped by label, not raw id


def test_report_scoping_user_vs_designer(app, client):
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    _add_field(client, tid, "amount", "integer")
    fid = _make_form(client, app, "lead_form", "Leads", tid)
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(row_owned="y"),
                    follow_redirects=True))
    amy = _new_amy(app, client)
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "boss", "amount": "100"},
                    follow_redirects=True))
    _ok(amy.post(f"/u/forms/{fid}/new", data={"name": "amy", "amount": "5"},
                 follow_redirects=True))

    q = "metric=count&metric=sum:amount&export=csv"
    amy_row = _csv_rows(amy.get(f"/u/report/{tid}?{q}").get_data(as_text=True))
    boss_row = _csv_rows(client.get(f"/u/report/{tid}?{q}").get_data(as_text=True))
    # no grouping -> a single totals row (keyed by the count value in _csv_rows)
    assert ["1", "5"] in list(amy_row.values())        # amy sees only her own row
    assert ["2", "105"] in list(boss_row.values())     # designer sees all rows
    # the Designer-mode report also sees everything
    drows = _csv_rows(client.get(
        f"/designer/report?table_id={tid}&{q}").get_data(as_text=True))
    assert any(r[0] == "2" for r in drows.values())


def test_saved_reports(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _make_form(client, app, "sale_form", "Sales", tid)       # so a plain user can read it

    _ok(client.post(f"/u/reports/{tid}",
                    data={"name": "By tier", "query": "group=tier&metric=count",
                          "next": f"/u/report/{tid}"}, follow_redirects=True))
    assert "By tier" in client.get(f"/u/report/{tid}").get_data(as_text=True)
    with app.app_context():
        rid = SessionLocal().scalar(select(ReportDef)).id

    amy = _new_amy(app, client)
    assert "By tier" not in amy.get(f"/u/report/{tid}").get_data(as_text=True)   # per-user

    _ok(client.post(f"/u/reports/{rid}/delete", follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(ReportDef)) is None


def _mint(app, username, name="t"):
    from app.api.tokens import mint
    from app.metadata.models import AppUser
    with app.app_context():
        uid = SessionLocal().scalar(select(AppUser).where(AppUser.username == username)).id
        _tok, raw = mint(SessionLocal(), uid, name)
    return {"Authorization": f"Bearer {raw}"}


def test_api_crud_and_auth(app, client):
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == "widget")).id
    _add_field(client, tid, "qty", "integer")
    H = _mint(app, "boss")
    api = app.test_client()                       # fresh client: token-only (no session)

    assert api.get("/api/v1/widget").status_code == 401
    assert api.get("/api/v1/widget",
                   headers={"Authorization": "Bearer nope"}).status_code == 401
    _ok(api.get("/api/v1/widget", headers=H))

    r = api.post("/api/v1/widget", json={"name": "A", "qty": 5}, headers=H)
    assert r.status_code == 201, r.get_data(as_text=True)
    obj = r.get_json()
    pk = obj["id"]
    assert obj["name"] == "A" and obj["qty"] == 5
    assert r.headers.get("Location", "").endswith(f"/api/v1/widget/{pk}")

    assert api.get(f"/api/v1/widget/{pk}", headers=H).get_json()["name"] == "A"
    lst = api.get("/api/v1/widget", headers=H).get_json()
    assert lst["total"] == 1 and lst["data"][0]["id"] == pk

    assert api.patch(f"/api/v1/widget/{pk}", json={"qty": 9}, headers=H).get_json()["qty"] == 9

    assert api.post("/api/v1/widget", json={"name": "B", "nope": 1}, headers=H).status_code == 400
    assert api.post("/api/v1/widget", json={"name": "B", "qty": "abc"}, headers=H).status_code == 400

    assert api.delete(f"/api/v1/widget/{pk}", headers=H).status_code == 204
    assert api.get(f"/api/v1/widget/{pk}", headers=H).status_code == 404


def test_api_permissions_and_ownership(app, client):
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    fid = _make_form(client, app, "lead_form", "Leads", tid)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    A = _mint(app, "amy", "amy")
    B = _mint(app, "boss", "boss")
    api = app.test_client()

    _ok(client.post("/designer/permissions", data={f"access_{fid}": "read"}, follow_redirects=True))
    _ok(api.get("/api/v1/lead", headers=A))                       # read allowed
    assert api.post("/api/v1/lead", json={"name": "x"}, headers=A).status_code == 403

    _ok(client.post("/designer/permissions", data={f"access_{fid}": "write"}, follow_redirects=True))
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(row_owned="y"), follow_redirects=True))
    assert api.post("/api/v1/lead", json={"name": "amy-row"}, headers=A).status_code == 201
    assert api.post("/api/v1/lead", json={"name": "boss-row"}, headers=B).status_code == 201

    amy_names = {r["name"] for r in api.get("/api/v1/lead", headers=A).get_json()["data"]}
    assert amy_names == {"amy-row"}                               # ownership scoping
    boss_names = {r["name"] for r in api.get("/api/v1/lead", headers=B).get_json()["data"]}
    assert {"amy-row", "boss-row"} <= boss_names                  # designer sees all


def test_api_token_lifecycle(app, client):
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    from app.metadata.models import ApiToken
    H = _mint(app, "boss")
    api = app.test_client()
    _ok(api.get("/api/v1/widget", headers=H))
    with app.app_context():
        tok_id = SessionLocal().scalar(select(ApiToken)).id

    _ok(client.post(f"/u/tokens/{tok_id}/revoke", follow_redirects=True))   # owner revokes via UI
    assert api.get("/api/v1/widget", headers=H).status_code == 401          # revoked → unauthorized

    # a different user cannot revoke someone else's token
    amy = _new_amy(app, client)
    H2 = _mint(app, "boss", "t2")
    with app.app_context():
        tok2 = SessionLocal().scalars(select(ApiToken).order_by(ApiToken.id.desc())).first().id
    _ok(amy.post(f"/u/tokens/{tok2}/revoke", follow_redirects=True))
    with app.app_context():
        assert SessionLocal().get(ApiToken, tok2).revoked is False
    _ok(api.get("/api/v1/widget", headers=H2))                              # still works


def _make_workflow(client, app, field_id, transitions, initial):
    from app.metadata.models import Workflow
    _ok(client.post("/designer/workflows", data={"field_id": field_id}, follow_redirects=True))
    with app.app_context():
        wid = SessionLocal().scalar(select(Workflow).where(Workflow.field_id == field_id)).id
    _ok(client.post(f"/designer/workflows/{wid}",
                    json={"transitions": transitions, "layout": {}, "initial": initial}))
    return wid


def _status_field_id(app, table_phys="ticket"):
    with app.app_context():
        t = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        return next(f.id for f in t.fields if f.phys_name == "status")


def test_workflow_transitions_ui(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nreview\napproved")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    sid = _status_field_id(app)
    wid = _make_workflow(client, app, sid,
                         [{"from": "draft", "to": "review", "roles": []},
                          {"from": "review", "to": "approved", "roles": []}], "draft")

    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "T1", "status": "draft"},
                    follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM ticket LIMIT 1")).scalar()

    def _status():
        with app.app_context():
            with get_engine().connect() as c:
                return c.execute(text("SELECT status FROM ticket WHERE id=:i"), {"i": pk}).scalar()

    # illegal jump draft -> approved is rejected (unchanged)
    _ok(client.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "T1", "status": "approved"},
                    follow_redirects=True))
    assert _status() == "draft"
    # allowed draft -> review saves
    _ok(client.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "T1", "status": "review"},
                    follow_redirects=True))
    assert _status() == "review"

    # the inline status dropdown is limited to valid next states (and offers no blank)
    list_html = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert 'data-noblank="1"' in list_html

    # inline cell editing must honour the workflow too
    cell = f"/u/forms/{fid}/{pk}/cell"
    assert client.post(cell, data={"col": "status", "value": "draft"}).status_code == 409
    assert _status() == "review"
    _ok(client.post(cell, data={"col": "status", "value": "approved"}))   # review -> approved allowed
    assert _status() == "approved"

    ed = client.get(f"/designer/workflows/{wid}").get_data(as_text=True)
    assert "wf-graph" in ed and "draft" in ed and "approved" in ed


def test_workflow_api_and_role_gate(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nreview\napproved")
    _make_form(client, app, "ticket_form", "Tickets", tid)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    sid = _status_field_id(app)
    _make_workflow(client, app, sid,
                   [{"from": "draft", "to": "review", "roles": []},
                    {"from": "review", "to": "approved", "roles": ["designer"]}], "draft")
    A = _mint(app, "amy")
    B = _mint(app, "boss")
    api = app.test_client()

    # create at any state (not transition-checked)
    r = api.post("/api/v1/ticket", json={"title": "T", "status": "review"}, headers=A)
    assert r.status_code == 201
    pk = r.get_json()["id"]

    # review -> approved is designer-only: user 409, designer 200
    assert api.patch(f"/api/v1/ticket/{pk}", json={"status": "approved"}, headers=A).status_code == 409
    assert api.patch(f"/api/v1/ticket/{pk}", json={"status": "approved"}, headers=B).status_code == 200
    # undefined edge approved -> draft → 409
    assert api.patch(f"/api/v1/ticket/{pk}", json={"status": "draft"}, headers=B).status_code == 409

    # an allowed edge any role may take
    pk2 = api.post("/api/v1/ticket", json={"title": "T2", "status": "draft"}, headers=A).get_json()["id"]
    assert api.patch(f"/api/v1/ticket/{pk2}", json={"status": "review"}, headers=A).status_code == 200


def test_workflow_schema_roundtrip(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nreview")
    sid = _status_field_id(app)
    _make_workflow(client, app, sid, [{"from": "draft", "to": "review", "roles": []}], "draft")

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        from app.metadata.models import Workflow
        wf = SessionLocal().scalar(select(Workflow))
        assert wf is not None and wf.initial_state == "draft"
        trans = json.loads(wf.transitions or "[]")
        assert any(t["from"] == "draft" and t["to"] == "review" for t in trans)


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


def _fid(app, table_phys, col):
    with app.app_context():
        t = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        return next(f.id for f in t.fields if f.phys_name == col)


def _make_trigger(app, table_phys, **kw):
    from app.metadata.models import TriggerRule
    with app.app_context():
        s = SessionLocal()
        t = s.scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        tr = TriggerRule(table_id=t.id, name=kw.pop("name", "rule"),
                         active=kw.pop("active", True), event=kw.pop("event", "update"), **kw)
        s.add(tr)
        s.commit()
        return tr.id


def test_triggers_fire_actions(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\ntriaged\nresolved")
    _add_field(client, tid, "resolved_on", "date")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    status_fid = _status_field_id(app)
    resolved_fid = _fid(app, "ticket", "resolved_on")
    from app.metadata.models import AppUser, Notification
    with app.app_context():
        boss_id = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss")).id
    _make_trigger(app, "ticket", name="On resolve", event="transition", field_id=status_fid,
                  to_state="resolved", in_app=True, notify_target="actor",
                  message="{title} resolved", set_field_id=resolved_fid, set_value="today",
                  email_to="ops@example.com", email_subject="{title}", email_body="done",
                  webhook_url="http://example.invalid/hook")

    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "T1", "status": "new"},
                    follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM ticket LIMIT 1")).scalar()
    _ok(client.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "T1", "status": "resolved"},
                    follow_redirects=True))

    with app.app_context():
        with get_engine().connect() as c:
            ro = c.execute(text("SELECT resolved_on FROM ticket WHERE id=:i"), {"i": pk}).scalar()
        notifs = SessionLocal().scalars(
            select(Notification).where(Notification.row_pk == pk)).all()
    assert ro == date.today()                                  # set_field action ran
    chans = {n.channel for n in notifs}
    assert {"in_app", "email", "webhook", "set_field"} <= chans
    inapp = next(n for n in notifs if n.channel == "in_app")
    assert inapp.user_id == boss_id and inapp.status == "unread" and inapp.body == "T1 resolved"
    assert all(n.status == "skipped" for n in notifs if n.channel in ("email", "webhook"))

    assert "T1 resolved" in client.get("/u/notifications").get_data(as_text=True)
    assert "🔔" in client.get("/u/").get_data(as_text=True)   # bell in the topbar
    _ok(client.post("/u/notifications/read", follow_redirects=True))
    with app.app_context():
        n = SessionLocal().scalar(select(Notification).where(Notification.channel == "in_app"))
        assert n.status == "read"


def test_trigger_event_scoping(app, client):
    _setup(client)
    tid = _make_table(client, app, "thing", "Thing", "name")
    _add_field(client, tid, "status", "enum", enum_options="a\nb")
    fid = _make_form(client, app, "thing_form", "Things", tid)
    from app.metadata.models import Notification
    _make_trigger(app, "thing", name="oncreate", event="create", in_app=True,
                  notify_target="actor", message="new {name}")

    def n_inapp():
        with app.app_context():
            return SessionLocal().scalar(select(func.count()).select_from(Notification)
                                         .where(Notification.channel == "in_app"))

    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "X", "status": "a"}, follow_redirects=True))
    assert n_inapp() == 1                                       # fired on create
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM thing LIMIT 1")).scalar()
    _ok(client.post(f"/u/forms/{fid}/{pk}/edit", data={"name": "Y", "status": "a"},
                    follow_redirects=True))
    assert n_inapp() == 1                                       # an update does NOT fire a create rule


def test_trigger_designer_ui_and_roundtrip(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\nresolved")
    _make_form(client, app, "ticket_form", "Tickets", tid)
    from app.metadata.models import TriggerRule

    _ok(client.get("/designer/triggers"))
    _ok(client.post("/designer/triggers", data={"table_id": tid}, follow_redirects=True))
    with app.app_context():
        rid = SessionLocal().scalar(select(TriggerRule)).id
    _ok(client.get(f"/designer/triggers/{rid}"))
    _ok(client.post(f"/designer/triggers/{rid}",
                    data={"name": "R1", "event": "create", "in_app": "y", "notify_target": "actor",
                          "message": "hi", "field_id": "0", "cond_field_id": "0",
                          "set_field_id": "0", "notify_user_id": "0", "cond_op": ""},
                    follow_redirects=True))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        tr = SessionLocal().scalar(select(TriggerRule))
        assert tr is not None and tr.name == "R1" and tr.event == "create"


def test_chart_data_shape():
    from app import reporting
    grouped = {"grouped": True, "group_label": "Tier", "titles": ["Count", "Sum of Amount"],
               "rows": [["gold", 2, 150], ["silver", 1, 30]], "totals": [3, 180]}
    cd = reporting.chart_data(grouped)
    assert cd["labels"] == ["gold", "silver"]
    assert cd["series"][0]["name"] == "Count" and cd["series"][0]["values"] == [2.0, 1.0]
    assert cd["series"][1]["values"] == [150.0, 30.0]

    ungrouped = {"grouped": False, "group_label": None, "titles": ["Count", "Sum"],
                 "rows": [[5, 99]], "totals": None}
    cu = reporting.chart_data(ungrouped)
    assert cu["labels"] == ["Count", "Sum"] and cu["series"][0]["values"] == [5.0, 99.0]


def test_report_chart_render(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _add_field(client, tid, "amount", "integer")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO sale (name, tier, amount) VALUES "
                           "('a','gold',100),('b','silver',30)"))

    html = client.get(f"/u/report/{tid}?group=tier&metric=count&chart=bar").get_data(as_text=True)
    assert 'class="js-chart"' in html and 'data-type="bar"' in html and "charts.js" in html
    assert "gold" in html                                   # chart JSON labels
    assert 'class="js-chart"' not in \
        client.get(f"/u/report/{tid}?group=tier&metric=count&chart=table").get_data(as_text=True)
    d = client.get(f"/designer/report?table_id={tid}&group=tier&metric=count&chart=pie")
    assert 'data-type="pie"' in d.get_data(as_text=True)


def test_dashboard_pin_flow(app, client):
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "name")
    _add_field(client, tid, "tier", "enum", enum_options="gold\nsilver")
    _make_form(client, app, "sale_form", "Sales", tid)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO sale (name, tier) VALUES ('a','gold'),('b','silver')"))
    _ok(client.post(f"/u/reports/{tid}",
                    data={"name": "By tier", "query": "group=tier&metric=count&chart=bar",
                          "next": f"/u/report/{tid}"}, follow_redirects=True))
    with app.app_context():
        rid = SessionLocal().scalar(select(ReportDef)).id

    assert "By tier" not in client.get("/u/").get_data(as_text=True)   # not pinned yet
    _ok(client.post(f"/u/reports/{rid}/pin", follow_redirects=True))
    home = client.get("/u/").get_data(as_text=True)
    assert "By tier" in home and 'class="js-chart"' in home and 'data-type="bar"' in home

    amy = _new_amy(app, client)
    assert "By tier" not in amy.get("/u/").get_data(as_text=True)      # per-user

    _ok(client.post(f"/u/reports/{rid}/pin", follow_redirects=True))   # unpin
    assert "By tier" not in client.get("/u/").get_data(as_text=True)


def test_custom_role_permissions(app, client):
    _setup(client)
    _ok(client.post("/designer/roles", data={"name": "viewer", "label": "Viewer"},
                    follow_redirects=True))
    tid = _make_table(client, app, "doc", "Doc", "title")
    fid = _make_form(client, app, "doc_form", "Docs", tid)
    _ok(client.post("/auth/users/new",
                    data=dict(username="vic", password="pw123456", role="viewer", is_active="y"),
                    follow_redirects=True))
    vic = app.test_client()
    _ok(vic.post("/auth/login", data=dict(username="vic", password="pw123456"),
                 follow_redirects=True))

    _ok(client.post("/designer/permissions", data={f"access_viewer_{fid}": "read"},
                    follow_redirects=True))
    _ok(vic.get(f"/u/forms/{fid}"))                                   # read ok
    assert vic.post(f"/u/forms/{fid}/new", data={"title": "x"}).status_code == 403
    _ok(client.post("/designer/permissions", data={f"access_viewer_{fid}": "none"},
                    follow_redirects=True))
    assert vic.get(f"/u/forms/{fid}").status_code == 403              # no read


def test_self_service_password(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    _ok(amy.get("/auth/account"))
    _ok(amy.post("/auth/account",
                 data={"current": "pw123456", "new": "newpass1", "confirm": "newpass1"},
                 follow_redirects=True))

    old = app.test_client()
    old.post("/auth/login", data=dict(username="amy", password="pw123456"), follow_redirects=True)
    assert old.get("/u/").status_code == 302                          # old password rejected
    new = app.test_client()
    _ok(new.post("/auth/login", data=dict(username="amy", password="newpass1"),
                 follow_redirects=True))
    _ok(new.get("/u/"))


def test_field_permissions(app, client):
    _setup(client)
    _ok(client.post("/designer/roles", data={"name": "viewer", "label": "Viewer"},
                    follow_redirects=True))
    tid = _make_table(client, app, "emp", "Emp", "name")
    _add_field(client, tid, "salary", "integer")
    _add_field(client, tid, "status", "enum", enum_options="active\nleft")
    fid = _make_form(client, app, "emp_form", "Emps", tid)
    salary_fid, status_fid = _fid(app, "emp", "salary"), _fid(app, "emp", "status")
    _ok(client.post(f"/designer/tables/{tid}/field-permissions",
                    data={f"facc_viewer_{salary_fid}": "none", f"facc_viewer_{status_fid}": "read"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "Ann", "salary": "100", "status": "active"}, follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM emp LIMIT 1")).scalar()

    _ok(client.post("/auth/users/new",
                    data=dict(username="vic", password="pw123456", role="viewer", is_active="y"),
                    follow_redirects=True))
    vic = app.test_client()
    _ok(vic.post("/auth/login", data=dict(username="vic", password="pw123456"),
                 follow_redirects=True))

    assert "Salary" not in vic.get(f"/u/forms/{fid}/new").get_data(as_text=True)   # field hidden
    assert "Salary" not in vic.get(f"/u/forms/{fid}").get_data(as_text=True)       # column hidden

    api = app.test_client()
    H = _mint(app, "vic")
    obj = api.get(f"/api/v1/emp/{pk}", headers=H).get_json()
    assert "salary" not in obj and "status" in obj and "name" in obj
    assert api.patch(f"/api/v1/emp/{pk}", json={"salary": 200}, headers=H).status_code == 400
    assert api.patch(f"/api/v1/emp/{pk}", json={"status": "left"}, headers=H).status_code == 400
    _ok(api.patch(f"/api/v1/emp/{pk}", json={"name": "Annie"}, headers=H))         # writable

    Hb = _mint(app, "boss")
    assert "salary" in api.get(f"/api/v1/emp/{pk}", headers=Hb).get_json()         # designer sees all


def test_access_control_roundtrip(app, client):
    _setup(client)
    _ok(client.post("/designer/roles", data={"name": "viewer", "label": "Viewer"},
                    follow_redirects=True))
    tid = _make_table(client, app, "emp", "Emp", "name")
    _add_field(client, tid, "salary", "integer")
    sid = _fid(app, "emp", "salary")
    _ok(client.post(f"/designer/tables/{tid}/field-permissions",
                    data={f"facc_viewer_{sid}": "none"}, follow_redirects=True))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        from app.metadata.models import MetaFieldPermission, Role
        s = SessionLocal()
        assert s.scalar(select(Role).where(Role.name == "viewer")) is not None
        assert s.scalar(select(MetaFieldPermission).where(MetaFieldPermission.access == "none")) \
            is not None


def test_sql_console_run_reject_and_export(app, client):
    _setup(client)
    _make_table(client, app, "note", "Note", "body")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO note (body) VALUES ('hello'),('world')"))

    _ok(client.get("/designer/query"))

    run = client.post("/designer/query",
                      data={"sql": "SELECT body FROM note ORDER BY id", "action": "run"})
    _ok(run)
    assert "hello" in run.get_data(as_text=True) and "world" in run.get_data(as_text=True)

    # a non-SELECT is rejected and does NOT execute
    rej = client.post("/designer/query", data={"sql": "DELETE FROM note", "action": "run"})
    _ok(rej)
    assert "Only SELECT" in rej.get_data(as_text=True)
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM note")).scalar() == 2

    exp = client.post("/designer/query",
                      data={"sql": "SELECT body FROM note ORDER BY id", "action": "export"})
    _ok(exp)
    assert exp.mimetype == "text/csv"
    body = exp.get_data(as_text=True)
    assert body.splitlines()[0] == "body" and "hello" in body


def test_sql_console_is_designer_only(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    assert amy.get("/designer/query").status_code == 403


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


# --------------------------------------------------------------------------- #
# New field types
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Chaining: HTTP connectors + feeds
# --------------------------------------------------------------------------- #
def _loopback(app):
    """Route connectors' HTTP through a fresh token-only test client (true loopback).

    The connection's base_url is irrelevant — only the path + Bearer token matter.
    Returns nothing; call ``connectors.set_transport(None)`` to restore.
    """
    from urllib.parse import urlsplit
    from app import connectors
    api = app.test_client()

    def transport(method, url, headers, body):
        u = urlsplit(url)
        path = u.path + (("?" + u.query) if u.query else "")
        resp = api.open(path, method=method, headers=dict(headers), data=body,
                        content_type=headers.get("Content-Type"))
        return resp.status_code, resp.get_data(as_text=True)

    connectors.set_transport(transport)


def _raw_token(app, username="boss"):
    return _mint(app, username)["Authorization"].split(" ", 1)[1]


def _make_connection(app, client, token, name="peer"):
    from app.metadata.models import Connection
    _ok(client.post("/designer/connections",
                    data={"name": name, "base_url": "http://self", "token": token, "active": "y"},
                    follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(Connection).where(Connection.name == name)).id


def test_connection_crud_and_ping(app, client):
    from app import connectors
    from app.metadata.models import Connection
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        with app.app_context():
            assert SessionLocal().get(Connection, cid).token == raw

        # Test button pings the peer and records the reachable tables
        r = client.post(f"/designer/connections/{cid}/test", follow_redirects=True)
        assert "widget" in r.get_data(as_text=True)

        # the /fields endpoint is reachable through the connector
        with app.app_context():
            fields = connectors.remote_fields(SessionLocal().get(Connection, cid), "widget")
        assert "name" in {f["name"] for f in fields}

        # editing with a blank token keeps the existing secret
        _ok(client.post(f"/designer/connections/{cid}",
                        data={"name": "peer2", "base_url": "http://self", "active": "y", "token": ""},
                        follow_redirects=True))
        with app.app_context():
            c = SessionLocal().get(Connection, cid)
            assert c.name == "peer2" and c.token == raw
    finally:
        connectors.set_transport(None)


def _make_feed_orm(app, source_tid, conn_id, target_table, field_map, **kw):
    from app.metadata.models import Feed
    with app.app_context():
        s = SessionLocal()
        feed = Feed(
            name=kw.get("name", "feed"), source_table_id=source_tid, connection_id=conn_id,
            target_table=target_table,
            field_map=json.dumps([{"source": a, "target": b} for a, b in field_map]),
            event=kw.get("event", "create"), mode=kw.get("mode", "create"),
            match_target_field=kw.get("match"), active=True,
            cond_field_id=kw.get("cond_field_id"), cond_op=kw.get("cond_op"),
            cond_value=kw.get("cond_value"), skip_api_writes=kw.get("skip_api_writes", True),
            allow_manual=kw.get("allow_manual", True),
            schedule_minutes=kw.get("schedule_minutes"))
        s.add(feed)
        s.commit()
        return feed.id


def test_feed_event_push(app, client):
    from app import connectors
    from app.metadata.models import Notification
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _make_table(client, app, "ordr", "Order", "name")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event="create")
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "Big deal"},
                        follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                names = [r[0] for r in c.execute(text("SELECT name FROM ordr")).all()]
            note = SessionLocal().scalar(
                select(Notification).where(Notification.channel == "feed"))
        assert names == ["Big deal"]                   # pushed through the real /api/v1
        assert note is not None and note.status == "sent"
    finally:
        connectors.set_transport(None)


def test_feed_upsert_and_workflow(app, client):
    from app import connectors
    from app.metadata.models import Notification
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _add_field(client, deal_tid, "ref", "string", length="40")
    _add_field(client, deal_tid, "stage", "string", length="40")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)

    order_tid = _make_table(client, app, "ordr", "Order", "name")
    _add_field(client, order_tid, "ext_ref", "string", length="40")
    _add_field(client, order_tid, "status", "enum", enum_options="new\nfulfilled\ncancelled")
    sid = _status_field_id(app, "ordr")
    _make_workflow(client, app, sid, [{"from": "new", "to": "fulfilled", "roles": []}], "new")

    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr",
                       [("title", "name"), ("ref", "ext_ref"), ("stage", "status")],
                       event="update", mode="upsert", match="ext_ref")
        _ok(client.post(f"/u/forms/{deal_fid}/new",
                        data={"title": "T1", "ref": "D1", "stage": "new"}, follow_redirects=True))
        with app.app_context():
            pk = get_engine().connect().execute(text("SELECT id FROM crm_deal LIMIT 1")).scalar()
        edit = f"/u/forms/{deal_fid}/{pk}/edit"

        def _order():
            with app.app_context():
                with get_engine().connect() as c:
                    return c.execute(text("SELECT name, status FROM ordr WHERE ext_ref='D1'")
                                     ).mappings().first()

        def _count():
            with app.app_context():
                with get_engine().connect() as c:
                    return c.execute(text("SELECT COUNT(*) FROM ordr")).scalar()

        # first update creates the order (upsert finds none) with status 'new'
        _ok(client.post(edit, data={"title": "T1", "ref": "D1", "stage": "new"},
                        follow_redirects=True))
        assert _count() == 1 and _order()["status"] == "new"

        # second update upserts the SAME order and drives new -> fulfilled
        _ok(client.post(edit, data={"title": "T1", "ref": "D1", "stage": "fulfilled"},
                        follow_redirects=True))
        assert _count() == 1                            # no duplicate
        assert _order()["status"] == "fulfilled"       # remote workflow transition applied

        # illegal fulfilled -> cancelled is rejected by the *remote* workflow
        _ok(client.post(edit, data={"title": "T1", "ref": "D1", "stage": "cancelled"},
                        follow_redirects=True))
        assert _order()["status"] == "fulfilled"       # unchanged
        with app.app_context():
            statuses = [n.status for n in SessionLocal().scalars(
                select(Notification).where(Notification.channel == "feed")
                .order_by(Notification.id))]
        assert "failed" in statuses
    finally:
        connectors.set_transport(None)


def test_feed_condition_gating(app, client):
    from app import connectors
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _add_field(client, deal_tid, "stage", "string", length="40")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event="create",
                       cond_field_id=_fid(app, "crm_deal", "stage"), cond_op="eq",
                       cond_value="won")
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "A", "stage": "open"},
                        follow_redirects=True))
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "B", "stage": "won"},
                        follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                names = [r[0] for r in c.execute(text("SELECT name FROM ordr ORDER BY id")).all()]
        assert names == ["B"]                           # only the 'won' deal pushed
    finally:
        connectors.set_transport(None)


def test_feed_designer_ui(app, client):
    from app.metadata.models import Feed
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _make_table(client, app, "ordr", "Order", "name")
    cid = _make_connection(app, client, _raw_token(app))

    _ok(client.post("/designer/feeds",
                    data={"table_id": deal_tid, "connection_id": cid}, follow_redirects=True))
    with app.app_context():
        fid = SessionLocal().scalar(select(Feed)).id
    _ok(client.get(f"/designer/feeds/{fid}"))           # edit page renders
    _ok(client.post(f"/designer/feeds/{fid}", data={
        "name": "Deal to Order", "connection_id": cid, "target_table": "ordr",
        "mode": "create", "event": "create", "field_id": 0, "cond_field_id": 0,
        "cond_op": "", "active": "y", "allow_manual": "y", "skip_api_writes": "y",
        "map_source": ["title", "stage"], "map_target": ["name", ""]}, follow_redirects=True))
    with app.app_context():
        feed = SessionLocal().get(Feed, fid)
        assert feed.name == "Deal to Order" and feed.event == "create"
        assert json.loads(feed.field_map) == [{"target": "name", "source": "title"}]


def test_feed_manual_send(app, client):
    from app import connectors
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    _make_form_p(client, app, "deal_view", "Deal", deal_tid, "view")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        # manual-only feed (no live event)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event=None)
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "A"}, follow_redirects=True))
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "B"}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                assert c.execute(text("SELECT COUNT(*) FROM ordr")).scalar() == 0   # no live push
                ids = [r[0] for r in c.execute(text("SELECT id FROM crm_deal ORDER BY id")).all()]

        # bulk 'Send to tools' pushes both selected rows
        _ok(client.post(f"/u/forms/{deal_fid}/send",
                        data={"ids": [str(i) for i in ids]}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                names = sorted(r[0] for r in c.execute(text("SELECT name FROM ordr")).all())
        assert names == ["A", "B"]

        # the single-record view page offers the same action
        v = client.get(f"/u/view/{deal_tid}/{ids[0]}").get_data(as_text=True)
        assert "Send to tools" in v
    finally:
        connectors.set_transport(None)


def test_feed_scheduled(app, client):
    from app import connectors, feeds as feeds_mod
    from app.metadata.models import Feed
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        feed_id = _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")],
                                 event=None, schedule_minutes=15)
        for t in ("A", "B"):
            _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": t}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                ids = [r[0] for r in c.execute(text("SELECT id FROM crm_deal ORDER BY id")).all()]

        def _names():
            with app.app_context():
                with get_engine().connect() as c:
                    return sorted(r[0] for r in c.execute(text("SELECT name FROM ordr")).all())

        # due (never run) -> pushes both rows; the watermark advances
        with app.app_context():
            assert feeds_mod.run_scheduled(SessionLocal(), get_engine()) == 2
            assert SessionLocal().get(Feed, feed_id).watermark == max(ids)
        assert _names() == ["A", "B"]

        # not due again so soon -> no-op (schedule gate)
        with app.app_context():
            assert feeds_mod.run_scheduled(SessionLocal(), get_engine()) == 0

        # 'Run now' bypasses the schedule and pushes only rows past the watermark
        _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": "C"}, follow_redirects=True))
        _ok(client.post(f"/designer/feeds/{feed_id}/run", follow_redirects=True))
        assert _names() == ["A", "B", "C"]

        # the `flask sync` CLI runs without error (now an alias for run-jobs)
        assert "Ran jobs" in app.test_cli_runner().invoke(args=["sync"]).output
    finally:
        connectors.set_transport(None)


def test_feed_schema_roundtrip(app, client):
    from app.metadata.models import Connection, Feed
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    _make_table(client, app, "ordr", "Order", "name")
    cid = _make_connection(app, client, "secret-token-123")
    _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")], event="create",
                   mode="upsert", match="name")

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    data = json.loads(exp.get_data())
    assert data["connections"] and "token" not in data["connections"][0]   # secret redacted
    assert data["feeds"] and data["feeds"][0]["target_table"] == "ordr"

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        conn = s.scalar(select(Connection))
        feed = s.scalar(select(Feed))
        assert conn is not None and conn.token is None          # re-entered after import
        assert feed is not None and feed.target_table == "ordr" and feed.mode == "upsert"
        assert json.loads(feed.field_map) == [{"source": "title", "target": "name"}]
        assert feed.connection_id == conn.id                    # re-wired to imported ids
        assert feed.source_table_id == s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "crm_deal")).id


def test_feed_loop_guard(app, client):
    """A feed on the target table must not re-fire when the write arrived via the API."""
    from app import connectors
    _setup(client)
    a_tid = _make_table(client, app, "tool_a", "A", "name")
    b_tid = _make_table(client, app, "tool_b", "B", "name")
    a_fid = _make_form(client, app, "a_form", "A", a_tid)
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, a_tid, cid, "tool_b", [("name", "name")], event="create")
        # back-feed B -> A, guarded against API-originated writes (the default)
        _make_feed_orm(app, b_tid, cid, "tool_a", [("name", "name")], event="create",
                       skip_api_writes=True)
        _ok(client.post(f"/u/forms/{a_fid}/new", data={"name": "X"}, follow_redirects=True))
        with app.app_context():
            with get_engine().connect() as c:
                a_count = c.execute(text("SELECT COUNT(*) FROM tool_a")).scalar()
                b_count = c.execute(text("SELECT COUNT(*) FROM tool_b")).scalar()
        assert a_count == 1 and b_count == 1            # A pushed to B; B did NOT loop back
    finally:
        connectors.set_transport(None)


# --------------------------------------------------------------------------- #
# In-app Help manuals
# --------------------------------------------------------------------------- #
def test_help_pages(app, client):
    _setup(client)                                      # boss = designer, logged in

    # /help redirects to the user manual
    r = client.get("/help")
    assert r.status_code in (301, 302) and "/help/user" in r.headers["Location"]

    # both manuals render for a designer; the topbar exposes the Help link
    u = client.get("/help/user")
    _ok(u)
    ub = u.get_data(as_text=True)
    assert "User Manual" in ub and 'title="Help"' in ub
    d = client.get("/help/designer")
    _ok(d)
    assert "Designer Manual" in d.get_data(as_text=True)

    # setup / developer / schema manuals also render for a designer
    for topic in ("setup", "developer", "schema"):
        _ok(client.get(f"/help/{topic}"))

    assert client.get("/help/bogus").status_code == 404

    # a normal user may read the user manual but not the designer-only topics
    amy = _new_amy(app, client)
    _ok(amy.get("/help/user"))
    for topic in ("designer", "setup", "developer", "schema"):
        assert amy.get(f"/help/{topic}").status_code == 403


def test_workflow_roles_include_custom(app, client):
    """A custom role must be selectable when gating a workflow transition."""
    import re
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nreview\napproved")
    _make_form(client, app, "ticket_form", "Tickets", tid)
    sid = _status_field_id(app)
    wid = _make_workflow(client, app, sid,
                         [{"from": "draft", "to": "review", "roles": []}], "draft")

    _ok(client.post("/designer/roles", data={"name": "approver", "label": "Approver"},
                    follow_redirects=True))

    html = client.get(f"/designer/workflows/{wid}").get_data(as_text=True)
    graph = json.loads(re.search(r'id="wf-graph">(.*?)</script>', html, re.S).group(1))
    # every role is offered — custom + both builtins (designer gates a
    # designer-only transition; it isn't filtered out here)
    assert {"approver", "user", "designer"} <= set(graph["roles"])

    # saving a transition gated to the custom role must persist it (not be stripped)
    _ok(client.post(f"/designer/workflows/{wid}", json={
        "transitions": [{"from": "draft", "to": "review", "roles": ["approver"]}],
        "layout": {}, "initial": "draft"}))
    html2 = client.get(f"/designer/workflows/{wid}").get_data(as_text=True)
    graph2 = json.loads(re.search(r'id="wf-graph">(.*?)</script>', html2, re.S).group(1))
    assert graph2["transitions"][0]["roles"] == ["approver"]


# --------------------------------------------------------------------------- #
# Clickable references
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Schema export/import completeness
# --------------------------------------------------------------------------- #
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
    from app.metadata.models import MetaForm, Sequence
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


# --------------------------------------------------------------------------- #
# Formula engine (pure — no DB)
# --------------------------------------------------------------------------- #
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


def _scalar(app, sql, **params):
    with app.app_context():
        with get_engine().connect() as c:
            return c.execute(text(sql), params).scalar()


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


# --------------------------------------------------------------------------- #
# Adopt existing (external) tables
# --------------------------------------------------------------------------- #
def _make_existing_tables(app):
    """Stand up non-Biggy tables (an existing app) directly via SQL."""
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("CREATE TABLE dept (id INT PRIMARY KEY AUTO_INCREMENT, "
                           "name VARCHAR(80))"))
            c.execute(text("CREATE TABLE emp (id INT PRIMARY KEY AUTO_INCREMENT, "
                           "name VARCHAR(80), salary DECIMAL(10,2), dept_id INT, "
                           "CONSTRAINT fk_emp_dept FOREIGN KEY (dept_id) REFERENCES dept(id))"))
            c.execute(text("CREATE TABLE legacy (a INT, b INT, descr VARCHAR(80), "
                           "PRIMARY KEY (a, b))"))
            c.execute(text("CREATE TABLE note (id INT PRIMARY KEY AUTO_INCREMENT, body VARCHAR(200))"))
            c.execute(text("INSERT INTO dept (id, name) VALUES (1, 'Sales')"))


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


# --------------------------------------------------------------------------- #
# Multiple data sources
# --------------------------------------------------------------------------- #
def _make_source(app, client, src2, name="src2"):
    from app.metadata.models import DataSource
    p = src2["params"]
    _ok(client.post("/designer/sources", data={
        "name": name, "driver": p["driver"], "host": p["host"], "port": str(p["port"]),
        "username": p["username"], "password": p["password"], "database": p["database"],
        "active": "y"}, follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(DataSource).where(DataSource.name == name)).id


def _home_tables(app):
    with app.app_context():
        with get_engine().connect() as c:
            return {r[0] for r in c.execute(text("SHOW TABLES")).all()}


def _make_source_generic(app, client, params, name):
    """Create a DataSource from a params dict, omitting blank parts (e.g. SQLite)."""
    from app.metadata.models import DataSource
    data = {"name": name, "driver": params["driver"], "active": "y"}
    for k in ("host", "port", "username", "password", "database"):
        if params.get(k) is not None:
            data[k] = str(params[k])
    _ok(client.post("/designer/sources", data=data, follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(DataSource).where(DataSource.name == name)).id


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


# --------------------------------------------------------------------------- #
# Any database backend (SQLite, via a data source) — dialect-agnostic DDL
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Arbitrary primary keys (single-column, any name/type)
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# Inbound webhooks (receive events from external systems)
# --------------------------------------------------------------------------- #
def _add_field(client, tid, phys, dtype, **extra):
    data = dict(phys_name=phys, label=phys.title(), data_type=dtype, nullable="y", **extra)
    _ok(client.post(f"/designer/tables/{tid}/fields", data=data, follow_redirects=True))


def _make_webhook(client, app, tid, pairs, mode="create", match_field=None, secret=None,
                  max_body_bytes=None, rate_limit=None, rate_window=None):
    """Create a webhook for ``tid`` mapping ``pairs`` of (json_path, target_col).

    Returns ``(webhook_id, raw_token)`` — the token is parsed from the one-time
    receive URL shown after creation. Optional limit fields are set via the form.
    """
    from app.metadata.models import Webhook
    resp = client.post("/designer/webhooks", data={"table_id": tid}, follow_redirects=True)
    _ok(resp)
    token = re.search(r"/hooks/(whk_[A-Za-z0-9_-]+)", resp.get_data(as_text=True)).group(1)
    with app.app_context():
        wid = SessionLocal().scalar(select(Webhook).order_by(Webhook.id.desc())).id
    data = {"name": "hook", "active": "y", "mode": mode, "user_id": 0,
            "map_source": [s for s, _ in pairs], "map_target": [t for _, t in pairs]}
    if match_field:
        data["match_field"] = match_field
    if secret:
        data["secret"] = secret
    if max_body_bytes is not None:
        data["max_body_bytes"] = max_body_bytes
    if rate_limit is not None:
        data["rate_limit"] = rate_limit
    if rate_window is not None:
        data["rate_window"] = rate_window
    _ok(client.post(f"/designer/webhooks/{wid}", data=data, follow_redirects=True))
    return wid, token


def test_webhook_receive_upsert_and_dotted_path(app, client):
    from app.metadata.models import Notification
    _setup(client)
    tid = _make_table(client, app, "lead", "Lead", "name")
    _add_field(client, tid, "email", "email", length=120)
    _add_field(client, tid, "score", "integer")
    # email comes from a nested path; upsert keyed on email
    _, token = _make_webhook(client, app, tid,
                             [("name", "name"), ("customer.email", "email"), ("score", "score")],
                             mode="upsert", match_field="email")

    r = client.post(f"/hooks/{token}",
                    json={"name": "Ada", "customer": {"email": "ada@x.com"}, "score": 7})
    assert r.status_code == 201 and r.get_json()["action"] == "create"
    with app.app_context():
        with get_engine().connect() as c:
            row = c.execute(text("SELECT name, email, score FROM lead")).mappings().all()
        assert len(row) == 1
        assert row[0]["name"] == "Ada" and row[0]["email"] == "ada@x.com" and row[0]["score"] == 7
        # delivery logged on the inbound channel
        assert SessionLocal().scalar(select(func.count()).select_from(Notification).where(
            Notification.channel == "webhook_in", Notification.status == "received")) == 1

    # second POST with the same key updates the same row (no duplicate)
    r2 = client.post(f"/hooks/{token}",
                     json={"name": "Ada L", "customer": {"email": "ada@x.com"}, "score": 9})
    assert r2.status_code == 200 and r2.get_json()["action"] == "update"
    with app.app_context():
        with get_engine().connect() as c:
            rows = c.execute(text("SELECT name, score FROM lead")).mappings().all()
        assert len(rows) == 1 and rows[0]["name"] == "Ada L" and rows[0]["score"] == 9


def test_webhook_hmac_and_unknown_token(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "subject")
    secret = "sh4red-s3cret"
    _, token = _make_webhook(client, app, tid, [("subject", "subject")], secret=secret)

    body = json.dumps({"subject": "Help"}).encode("utf-8")
    sign = lambda b: "sha256=" + hmac.new(secret.encode(), b, hashlib.sha256).hexdigest()

    # unsigned and badly-signed are rejected
    assert client.post(f"/hooks/{token}", data=body,
                       content_type="application/json").status_code == 401
    assert client.post(f"/hooks/{token}", data=body, content_type="application/json",
                       headers={"X-Biggy-Signature": "sha256=deadbeef"}).status_code == 401
    # a valid signature is accepted
    r = client.post(f"/hooks/{token}", data=body, content_type="application/json",
                    headers={"X-Biggy-Signature": sign(body)})
    assert r.status_code == 201
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT subject FROM ticket")).scalar() == "Help"

    # an unknown token is a 404, with nothing written
    assert client.post("/hooks/whk_does_not_exist", json={"subject": "X"}).status_code == 404
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM ticket")).scalar() == 1


def test_webhook_schema_roundtrip(app, client):
    from app.metadata.models import Webhook
    _setup(client)
    tid = _make_table(client, app, "signup", "Signup", "email")
    _make_webhook(client, app, tid, [("user.email", "email")], mode="upsert",
                  match_field="email", secret="top-secret",
                  max_body_bytes=2048, rate_limit=30, rate_window=10)

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    data = json.loads(exp.get_data())
    assert data["webhooks"] and data["webhooks"][0]["mode"] == "upsert"
    wb = data["webhooks"][0]
    assert "token_hash" not in wb and "secret" not in wb        # secrets redacted
    assert json.loads(wb["field_map"]) == [{"source": "user.email", "target": "email"}]
    assert wb["max_body_bytes"] == 2048 and wb["rate_limit"] == 30 and wb["rate_window"] == 10

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        wh = s.scalar(select(Webhook))
        assert wh is not None and wh.mode == "upsert" and wh.match_field == "email"
        assert wh.token_hash and wh.secret is None              # fresh token, secret re-entered
        assert wh.max_body_bytes == 2048 and wh.rate_limit == 30 and wh.rate_window == 10
        assert wh.target_table_id == s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "signup")).id


def test_webhook_payload_size_cap(app, client):
    _setup(client)
    tid = _make_table(client, app, "blurb", "Blurb", "body")
    # a per-webhook cap of 200 bytes (set via the designer form)
    _, token = _make_webhook(client, app, tid, [("body", "body")], max_body_bytes=200)

    big = client.post(f"/hooks/{token}", json={"body": "x" * 500})
    assert big.status_code == 413
    small = client.post(f"/hooks/{token}", json={"body": "ok"})
    assert small.status_code == 201
    with app.app_context():
        with get_engine().connect() as c:
            rows = c.execute(text("SELECT body FROM blurb")).scalars().all()
    assert rows == ["ok"]                                       # oversized one never written


def test_webhook_rate_limit(app, client):
    _setup(client)
    tid = _make_table(client, app, "ping", "Ping", "label")
    # allow 2 per window; the 3rd is throttled (set via the designer form)
    _, token = _make_webhook(client, app, tid, [("label", "label")],
                             rate_limit=2, rate_window=60)

    assert client.post(f"/hooks/{token}", json={"label": "a"}).status_code == 201
    assert client.post(f"/hooks/{token}", json={"label": "b"}).status_code == 201
    blocked = client.post(f"/hooks/{token}", json={"label": "c"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1            # tells the caller when to retry
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM ping")).scalar() == 2   # 'c' rejected


# --------------------------------------------------------------------------- #
# General scheduler (time-driven triggers / feeds / report digests)
# --------------------------------------------------------------------------- #
def _force_due(app, model_name, obj_id):
    """Reset a job's last_run_at so it is due again."""
    from app.metadata import models
    with app.app_context():
        s = SessionLocal()
        obj = s.get(getattr(models, model_name), obj_id)
        obj.last_run_at = None
        s.commit()


def test_scheduled_trigger_reminders(app, client):
    from app import scheduler
    from app.metadata.models import AppUser, Notification
    _setup(client)
    tid = _make_table(client, app, "task", "Task", "title")
    _add_field(client, tid, "reminded", "enum", enum_options="no\nyes")
    fid = _make_form(client, app, "task_form", "Tasks", tid)
    with app.app_context():
        boss_id = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss")).id

    # two un-reminded rows + one already handled
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "A"}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "B"}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "C", "reminded": "yes"},
                    follow_redirects=True))

    # scheduled rule: for rows where reminded != yes → notify + mark reminded=yes (idempotency guard)
    rid = _make_trigger(app, "task", name="Daily reminder", event="scheduled",
                        schedule_minutes=60, cond_field_id=_fid(app, "task", "reminded"),
                        cond_op="ne", cond_value="yes", in_app=True, notify_target="user",
                        notify_user_id=boss_id, message="reminder: {title}",
                        set_field_id=_fid(app, "task", "reminded"), set_value="yes")

    def _counts():
        with app.app_context():
            s = SessionLocal()
            inapp = s.scalars(select(Notification).where(Notification.channel == "in_app")).all()
            with get_engine().connect() as c:
                done = c.execute(text("SELECT COUNT(*) FROM task WHERE reminded='yes'")).scalar()
            return len(inapp), done, {n.body for n in inapp}

    # first pass: the 2 un-reminded rows fire (C already handled is skipped) and get marked
    with app.app_context():
        summary = scheduler.run_due(SessionLocal(), get_engine())
    assert summary["triggers"] == 2
    n_inapp, done, bodies = _counts()
    assert n_inapp == 2 and done == 3
    assert bodies == {"reminder: A", "reminder: B"} and all(b.startswith("reminder") for b in bodies)

    # immediate re-run: not due → no-op
    with app.app_context():
        assert scheduler.run_due(SessionLocal(), get_engine())["triggers"] == 0
    assert _counts()[0] == 2

    # force due again: now every row is reminded=yes → condition matches nothing (idempotent)
    _force_due(app, "TriggerRule", rid)
    with app.app_context():
        assert scheduler.run_due(SessionLocal(), get_engine())["triggers"] == 0
    assert _counts()[0] == 2                                    # still only the original two


def test_scheduler_runs_due_feeds(app, client):
    from app import connectors, scheduler
    _setup(client)
    deal_tid = _make_table(client, app, "crm_deal", "Deal", "title")
    deal_fid = _make_form(client, app, "deal_form", "Deals", deal_tid)
    _make_table(client, app, "ordr", "Order", "name")
    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        _make_feed_orm(app, deal_tid, cid, "ordr", [("title", "name")],
                       event=None, schedule_minutes=15)
        for t in ("A", "B"):
            _ok(client.post(f"/u/forms/{deal_fid}/new", data={"title": t}, follow_redirects=True))
        # run_due drives the scheduled feed (parity with feeds.run_scheduled)
        with app.app_context():
            summary = scheduler.run_due(SessionLocal(), get_engine())
        assert summary["feeds"] == 2
        with app.app_context():
            with get_engine().connect() as c:
                assert sorted(r[0] for r in c.execute(text("SELECT name FROM ordr"))) == ["A", "B"]
    finally:
        connectors.set_transport(None)


def test_scheduled_report_digest(app, client):
    from app import scheduler
    from app.metadata.models import Notification, ReportDef
    _setup(client)
    tid = _make_table(client, app, "sale", "Sale", "item")
    fid = _make_form(client, app, "sale_form", "Sales", tid)
    for it in ("x", "y"):
        _ok(client.post(f"/u/forms/{fid}/new", data={"item": it}, follow_redirects=True))

    # save a scheduled report (email digest) via the user UI
    _ok(client.post(f"/u/reports/{tid}",
                    data={"name": "Daily sales", "query": "", "schedule_minutes": "30",
                          "recipients": "ops@example.com"}, follow_redirects=True))
    with app.app_context():
        rid = SessionLocal().scalar(select(ReportDef)).id

    with app.app_context():
        summary = scheduler.run_due(SessionLocal(), get_engine())
    assert summary["reports"] == 1
    with app.app_context():
        s = SessionLocal()
        note = s.scalar(select(Notification).where(Notification.channel == "report"))
        rep = s.get(ReportDef, rid)
        assert note is not None and note.target == "ops@example.com"
        assert note.status == "skipped"                        # email skipped under TESTING
        assert rep.last_run_at is not None
    # not due on an immediate second pass
    with app.app_context():
        assert scheduler.run_due(SessionLocal(), get_engine())["reports"] == 0


def test_run_jobs_cli_and_ticker(app, client):
    from app import scheduler
    _setup(client)
    out = app.test_cli_runner().invoke(args=["run-jobs"]).output
    assert "Ran jobs" in out and "triggers" in out
    assert "Ran jobs" in app.test_cli_runner().invoke(args=["sync"]).output   # alias still works
    # tick_once runs a pass within an app context and reports the job kinds
    summary = scheduler.tick_once(app)
    assert set(summary) == {"triggers", "feeds", "pulls", "reports", "sla"}


def test_scheduled_trigger_roundtrip(app, client):
    from app.metadata.models import TriggerRule
    _setup(client)
    _make_table(client, app, "task", "Task", "title")
    _make_trigger(app, "task", name="Nightly", event="scheduled", schedule_minutes=120,
                  in_app=True, notify_target="actor", message="tick")

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    tr = json.loads(exp.get_data())["trigger_rules"][0]
    assert tr["event"] == "scheduled" and tr["schedule_minutes"] == 120

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        rule = SessionLocal().scalar(select(TriggerRule))
        assert rule.event == "scheduled" and rule.schedule_minutes == 120


# --------------------------------------------------------------------------- #
# Pull / polling connectors (ingest from peers & REST APIs)
# --------------------------------------------------------------------------- #
def _make_pull_orm(app, target_tid, field_map, **kw):
    from app.metadata.models import PullSource
    cfg = kw.get("config")
    with app.app_context():
        s = SessionLocal()
        ps = PullSource(
            name=kw.get("name", "pull"), target_table_id=target_tid, kind=kw.get("kind", "peer"),
            connection_id=kw.get("connection_id"), remote_table=kw.get("remote_table"),
            url=kw.get("url"), records_path=kw.get("records_path"), headers=kw.get("headers"),
            config=json.dumps(cfg) if isinstance(cfg, dict) else cfg,
            auth_secret=kw.get("auth_secret"), watermark=kw.get("watermark"),
            field_map=json.dumps([{"source": a, "target": b} for a, b in field_map]),
            mode=kw.get("mode", "upsert"), match_field=kw.get("match_field"),
            cursor_field=kw.get("cursor_field"), page_size=kw.get("page_size"),
            schedule_minutes=kw.get("schedule_minutes"), active=True)
        s.add(ps)
        s.commit()
        return ps.id


def test_pull_from_peer(app, client):
    from app import connectors, pull, scheduler
    from app.metadata.models import Notification, PullSource
    _setup(client)
    src_tid = _make_table(client, app, "widget", "Widget", "name")
    _add_field(client, src_tid, "sku", "string", length="40")
    src_fid = _make_form(client, app, "widget_form", "Widgets", src_tid)
    dest_tid = _make_table(client, app, "mirror", "Mirror", "title")
    _add_field(client, dest_tid, "code", "string", length="40")
    for nm, sku in [("Alpha", "A1"), ("Beta", "B2")]:
        _ok(client.post(f"/u/forms/{src_fid}/new", data={"name": nm, "sku": sku},
                        follow_redirects=True))

    raw = _raw_token(app)
    _loopback(app)
    try:
        cid = _make_connection(app, client, raw)
        pid = _make_pull_orm(app, dest_tid, [("name", "title"), ("sku", "code")],
                             kind="peer", connection_id=cid, remote_table="widget",
                             mode="upsert", match_field="code", cursor_field="id",
                             schedule_minutes=15)

        def _mirror():
            with app.app_context():
                with get_engine().connect() as c:
                    return sorted((r[0], r[1]) for r in
                                  c.execute(text("SELECT title, code FROM mirror")))

        # the scheduler runs the due pull → both remote rows land locally
        with app.app_context():
            assert scheduler.run_due(SessionLocal(), get_engine())["pulls"] == 2
        assert _mirror() == [("Alpha", "A1"), ("Beta", "B2")]
        with app.app_context():
            assert SessionLocal().scalar(select(func.count()).select_from(Notification).where(
                Notification.channel == "pull_in", Notification.status == "received")) >= 1

        # a new remote row → only it is pulled next time (watermark over id)
        _ok(client.post(f"/u/forms/{src_fid}/new", data={"name": "Gamma", "sku": "G3"},
                        follow_redirects=True))
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 1
        assert _mirror() == [("Alpha", "A1"), ("Beta", "B2"), ("Gamma", "G3")]

        # reset the watermark → all rows re-pulled but upserted on code (no duplicates)
        with app.app_context():
            s = SessionLocal()
            s.get(PullSource, pid).watermark = None
            s.commit()
            pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid)
        assert len(_mirror()) == 3
    finally:
        connectors.set_transport(None)


def test_pull_from_rest(app, client):
    from app import connectors, pull
    from app.metadata.models import PullSource
    _setup(client)
    tid = _make_table(client, app, "person", "Person", "name")
    _add_field(client, tid, "email", "email", length="120")

    payload = {"result": {"items": [
        {"id": 1, "name": "Ann", "user": {"email": "ann@x.com"}},
        {"id": 2, "name": "Bob", "user": {"email": "bob@x.com"}}]}}

    def transport(method, url, headers, body):
        assert "api.test" in url and headers.get("X-Key") == "sek"   # url + headers honoured
        return 200, json.dumps(payload)

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(app, tid, [("name", "name"), ("user.email", "email")],
                             kind="rest", url="http://api.test/people",
                             headers=json.dumps({"X-Key": "sek"}), records_path="result.items",
                             mode="upsert", match_field="email", cursor_field="id")

        def _people():
            with app.app_context():
                with get_engine().connect() as c:
                    return sorted((r[0], r[1]) for r in
                                  c.execute(text("SELECT name, email FROM person")))

        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 2
        assert _people() == [("Ann", "ann@x.com"), ("Bob", "bob@x.com")]   # nested dotted path

        # same canned response → cursor watermark filters everything out (no re-import)
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 0
        assert len(_people()) == 2
    finally:
        connectors.set_transport(None)


def test_pull_schema_roundtrip(app, client):
    from app.metadata.models import Connection, PullSource
    _setup(client)
    _make_table(client, app, "widget", "Widget", "name")
    dest_tid = _make_table(client, app, "mirror", "Mirror", "title")
    cid = _make_connection(app, client, "secret-token")
    _make_pull_orm(app, dest_tid, [("name", "title")], kind="peer", connection_id=cid,
                   remote_table="widget", mode="upsert", match_field="title",
                   cursor_field="id", schedule_minutes=20, headers=json.dumps({"X-Key": "s"}))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    ps = json.loads(exp.get_data())["pull_sources"][0]
    assert ps["remote_table"] == "widget" and ps["schedule_minutes"] == 20
    assert "headers" not in ps and "watermark" not in ps        # secret + runtime state redacted

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        src = s.scalar(select(PullSource))
        conn = s.scalar(select(Connection))
        assert src is not None and src.kind == "peer" and src.cursor_field == "id"
        assert src.headers is None and src.watermark is None    # re-entered / reset on import
        assert src.connection_id == conn.id                     # re-wired to the imported connection
        assert src.target_table_id == s.scalar(
            select(MetaTable).where(MetaTable.phys_name == "mirror")).id


def test_pull_rest_pagination_auth_templating(app, client):
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "thing", "Thing", "title")
    seen = {"auth": None, "urls": []}
    pages = {1: [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
             2: [{"id": 3, "name": "C"}], 3: []}

    def transport(method, url, headers, body):
        from urllib.parse import urlsplit, parse_qs
        seen["auth"] = headers.get("Authorization")
        seen["urls"].append(url)
        page = int(parse_qs(urlsplit(url).query).get("p", ["1"])[0])
        return 200, json.dumps({"items": pages.get(page, [])})

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(
            app, tid, [("name", "title")], kind="rest", url="http://api.test/things",
            records_path="items", mode="create", watermark="2026-01-01",
            config={"auth": {"type": "bearer"}, "request": {"params": {"since": "{watermark}"}},
                    "pagination": {"style": "page", "param": "p", "max_pages": 5}},
            auth_secret="sek")
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 3
        with app.app_context():
            with get_engine().connect() as c:
                assert sorted(r[0] for r in c.execute(text("SELECT title FROM thing"))) == \
                    ["A", "B", "C"]                              # collected across pages
        assert seen["auth"] == "Bearer sek"                      # auth preset built the header
        assert any("since=2026-01-01" in u for u in seen["urls"])   # {watermark} substituted
        assert any("p=2" in u for u in seen["urls"])             # actually paginated
    finally:
        connectors.set_transport(None)


def test_pull_rest_max_pages_cap(app, client):
    """A source whose pages never empty must stop at max_pages (no runaway)."""
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "blip", "Blip", "title")
    calls = {"n": 0}

    def transport(method, url, headers, body):
        calls["n"] += 1
        return 200, json.dumps({"items": [{"id": calls["n"], "name": f"r{calls['n']}"}]})

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(app, tid, [("name", "title")], kind="rest",
                             url="http://api.test/blip", records_path="items", mode="create",
                             config={"pagination": {"style": "page", "param": "p", "max_pages": 3}})
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 3
        assert calls["n"] == 3                                   # capped at max_pages, not infinite
    finally:
        connectors.set_transport(None)


def test_pull_rest_template_transform_filter(app, client):
    from app import connectors, pull
    _setup(client)
    tid = _make_table(client, app, "person", "Person", "fullname")
    _add_field(client, tid, "state", "string", length="20")
    body = {"items": [
        {"id": 1, "first": "Ada", "last": "L", "status": "A", "archived": False},
        {"id": 2, "first": "Bo", "last": "X", "status": "", "archived": False},
        {"id": 3, "first": "Zed", "last": "Q", "status": "A", "archived": True}]}

    def transport(method, url, headers, _body):
        return 200, json.dumps(body)

    connectors.set_transport(transport)
    try:
        pid = _make_pull_orm(
            app, tid, [("{first} {last}", "fullname"), ("status", "state")], kind="rest",
            url="http://api.test/p", records_path="items", mode="create",
            config={"filter": {"field": "archived", "op": "is_false"},
                    "transforms": {"state": {"map": {"A": "Active"}, "default": "pending"}}})
        with app.app_context():
            assert pull.run_scheduled(SessionLocal(), get_engine(), only_source_id=pid) == 2
        with app.app_context():
            with get_engine().connect() as c:
                rows = sorted((r[0], r[1]) for r in
                              c.execute(text("SELECT fullname, state FROM person")))
        # id=3 archived → filtered; {first} {last} template joins names; map A→Active; ""→default
        assert rows == [("Ada L", "Active"), ("Bo X", "pending")]
    finally:
        connectors.set_transport(None)


def test_pull_advanced_config_roundtrip(app, client):
    from app.metadata.models import PullSource
    _setup(client)
    dest = _make_table(client, app, "mirror", "Mirror", "title")
    _make_pull_orm(app, dest, [("name", "title")], kind="rest", url="http://api.test/x",
                   config={"auth": {"type": "bearer"}, "pagination": {"style": "page", "param": "p"}},
                   auth_secret="topsecret", headers=json.dumps({"X": "y"}))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    ps = json.loads(exp.get_data())["pull_sources"][0]
    assert json.loads(ps["config"])["pagination"]["style"] == "page"
    assert "auth_secret" not in ps and "headers" not in ps      # secrets redacted

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        src = SessionLocal().scalar(select(PullSource))
        assert json.loads(src.config)["auth"]["type"] == "bearer"   # config round-trips
        assert src.auth_secret is None and src.headers is None      # secrets re-entered on import


# --------------------------------------------------------------------------- #
# Dashboards & charts
# --------------------------------------------------------------------------- #
def _ticket_table(client, app):
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\nopen\ndone")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    for t, s in [("A", "new"), ("B", "open"), ("C", "open")]:
        _ok(client.post(f"/u/forms/{fid}/new", data={"title": t, "status": s},
                        follow_redirects=True))
    return tid, fid


def _make_shared_dashboard(client, app, name="Ops"):
    from app.metadata.models import Dashboard
    _ok(client.post("/designer/dashboards", data={"name": name}, follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(Dashboard).where(Dashboard.name == name)).id


def _add_widget(client, did, **d):
    base = {"kind": "chart", "table_id": 0, "query": "", "chart_type": "bar",
            "content": "", "width": "1", "limit": "5"}
    base.update(d)
    _ok(client.post(f"/designer/dashboards/{did}/widgets", data=base, follow_redirects=True))


def test_shared_dashboard_widgets(app, client):
    from app import dashboards
    from app.metadata.models import AppUser, Dashboard
    _setup(client)
    tid, _fid = _ticket_table(client, app)
    did = _make_shared_dashboard(client, app)
    _add_widget(client, did, kind="chart", title="By status", table_id=tid,
                query="group=status&metric=count", chart_type="bar", width="2")
    _add_widget(client, did, kind="number", title="Total", table_id=tid, query="metric=count")
    _add_widget(client, did, kind="list", title="Recent", table_id=tid, limit="2")
    _add_widget(client, did, kind="text", title="Note", content="# Heading\n\nHello world")

    with app.app_context():
        s = SessionLocal()
        boss = s.scalar(select(AppUser).where(AppUser.username == "boss"))
        tiles = {t["kind"]: t for t in dashboards.render(s, boss, s.get(Dashboard, did))}
        assert tiles["chart"]["chart_data"]["series"][0]["values"]      # grouped counts
        assert tiles["number"]["value"] == 3                            # total rows
        assert len(tiles["list"]["rows"]) == 2                          # row limit honoured
        assert "Hello world" in str(tiles["text"]["html"])              # markdown rendered

    assert client.get(f"/u/dashboards/{did}").status_code == 200        # page renders

    # a dashboard-kind menu links to it and shows in the nav
    _ok(client.post("/designer/menus/new",
                    data={"label": "Ops board", "kind": "dashboard", "parent_id": 0,
                          "target_dashboard_id": did, "position": 0}, follow_redirects=True))
    assert f"/u/dashboards/{did}" in client.get("/u/").get_data(as_text=True)


def test_dashboard_gating_and_personal(app, client):
    from app import dashboards
    from app.metadata.models import AppUser, Dashboard, DashboardWidget
    _setup(client)
    tid, _fid = _ticket_table(client, app)
    sec_tid = _make_table(client, app, "secret", "Secret", "code")
    sec_fid = _make_form(client, app, "secret_form", "Secrets", sec_tid)
    amy = _new_amy(app, client)

    # a shared dashboard mixing a readable (ticket) + a denied (secret) widget
    did = _make_shared_dashboard(client, app, "Mixed")
    _add_widget(client, did, kind="number", title="Tickets", table_id=tid, query="metric=count")
    _add_widget(client, did, kind="number", title="Secrets", table_id=sec_tid, query="metric=count")
    _ok(client.post("/designer/permissions", data={f"access_{sec_fid}": "none"},
                    follow_redirects=True))                       # deny the 'user' role on secret

    with app.app_context():
        s = SessionLocal()
        amy_user = s.scalar(select(AppUser).where(AppUser.username == "amy"))
        dash = s.get(Dashboard, did)
        assert dashboards.visible(s, amy_user, dash)              # the ticket widget is readable
        amy_kinds = dashboards.render(s, amy_user, dash)
        assert len(amy_kinds) == 1 and amy_kinds[0]["w"].table_id == tid   # secret widget hidden
    assert amy.get(f"/u/dashboards/{did}").status_code == 200

    # a dashboard with ONLY the denied widget is invisible to amy (404), visible to the designer
    only = _make_shared_dashboard(client, app, "Locked")
    _add_widget(client, only, kind="number", table_id=sec_tid, query="metric=count")
    assert amy.get(f"/u/dashboards/{only}").status_code == 404
    assert client.get(f"/u/dashboards/{only}").status_code == 200

    # personal dashboards: amy builds one; the designer can't see it
    amy.post("/u/dashboards/new", data={"name": "Mine"}, follow_redirects=True)
    with app.app_context():
        pid = SessionLocal().scalar(
            select(Dashboard).where(Dashboard.name == "Mine")).id
    _ok(amy.post(f"/u/dashboards/{pid}/widgets",
                 data={"kind": "number", "title": "My tickets", "table_id": tid,
                       "query": "metric=count"}, follow_redirects=True))
    assert amy.get(f"/u/dashboards/{pid}").status_code == 200
    assert client.get(f"/u/dashboards/{pid}").status_code == 404   # not the designer's
    with app.app_context():
        assert SessionLocal().scalar(select(func.count()).select_from(DashboardWidget)
                                     .where(DashboardWidget.dashboard_id == pid)) == 1


def test_dashboard_schema_roundtrip(app, client):
    from app.metadata.models import Dashboard, DashboardWidget, MetaMenu
    _setup(client)
    tid, _fid = _ticket_table(client, app)
    did = _make_shared_dashboard(client, app, "Board")
    _add_widget(client, did, kind="chart", title="By status", table_id=tid,
                query="group=status&metric=count", chart_type="pie", width="2")
    _ok(client.post("/designer/menus/new",
                    data={"label": "Board", "kind": "dashboard", "parent_id": 0,
                          "target_dashboard_id": did, "position": 0}, follow_redirects=True))
    # a personal dashboard must NOT be exported
    amy = _new_amy(app, client)
    amy.post("/u/dashboards/new", data={"name": "Personal"}, follow_redirects=True)

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    data = json.loads(exp.get_data())
    assert [d["name"] for d in data["dashboards"]] == ["Board"]      # shared only, personal excluded
    assert data["dashboard_widgets"][0]["chart_type"] == "pie"

    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        dash = s.scalar(select(Dashboard).where(Dashboard.owner_user_id.is_(None)))
        widget = s.scalar(select(DashboardWidget))
        menu = s.scalar(select(MetaMenu).where(MetaMenu.kind == "dashboard"))
        new_tid = s.scalar(select(MetaTable).where(MetaTable.phys_name == "ticket")).id
        assert dash.name == "Board" and widget.chart_type == "pie"
        assert widget.dashboard_id == dash.id and widget.table_id == new_tid   # remapped
        assert menu.target_dashboard_id == dash.id                  # menu target remapped


# --------------------------------------------------------------------------- #
# REST API hardening: OpenAPI spec + docs + bulk endpoints
# --------------------------------------------------------------------------- #
def test_api_openapi_and_docs(app, client):
    _setup(client)
    tid = _make_table(client, app, "widget", "Widget", "name")
    _add_field(client, tid, "qty", "integer")
    _add_field(client, tid, "status", "enum", enum_options="new\ndone")
    _ok(client.post(f"/designer/tables/{tid}/fields",                 # required (nullable omitted)
                    data=dict(phys_name="code", label="Code", data_type="string", length=40),
                    follow_redirects=True))
    H = _mint(app, "boss")
    api = app.test_client()

    spec = api.get("/api/v1/openapi.json", headers=H)
    assert spec.status_code == 200
    doc = spec.get_json()
    assert doc["openapi"].startswith("3.0")
    assert "/widget" in doc["paths"] and "/widget/bulk" in doc["paths"]
    assert {"post", "patch", "delete"} <= set(doc["paths"]["/widget/bulk"])
    props = doc["components"]["schemas"]["widget"]["properties"]
    assert props["qty"]["type"] == "integer"
    assert props["status"]["enum"] == ["new", "done"]
    assert props["id"]["readOnly"] is True                       # pk is read-only
    assert "code" in doc["components"]["schemas"]["widget"]["required"]   # non-nullable ⇒ required

    docs = api.get("/api/v1/docs", headers=H)
    assert docs.status_code == 200
    body = docs.get_data(as_text=True)
    assert "/api/v1/widget/bulk" in body and "widget" in body    # self-hosted reference renders


def test_api_bulk_operations(app, client):
    from app.metadata.models import AppUser, Notification
    _setup(client)
    tid = _make_table(client, app, "widget", "Widget", "name")
    _add_field(client, tid, "qty", "integer")
    fid = _make_form(client, app, "widget_form", "Widgets", tid)
    with app.app_context():
        boss_id = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss")).id
    # a create trigger proves the bulk path goes through record_service (triggers fire per row)
    _make_trigger(app, "widget", name="On add", event="create", in_app=True,
                  notify_target="user", notify_user_id=boss_id, message="new {name}")
    H = _mint(app, "boss")
    api = app.test_client()

    # bulk create: 3 valid rows
    r = api.post("/api/v1/widget/bulk",
                 json={"records": [{"name": "A", "qty": 1}, {"name": "B", "qty": 2},
                                   {"name": "C", "qty": 3}]}, headers=H)
    assert r.status_code == 201 and len(r.get_json()["created"]) == 3
    with app.app_context():
        assert SessionLocal().scalar(select(func.count()).select_from(Notification)
                                     .where(Notification.channel == "in_app")) == 3   # fired per row

    # one bad row → 207, the others still created
    r2 = api.post("/api/v1/widget/bulk",
                  json={"records": [{"name": "D", "qty": 4}, {"name": "E", "qty": "bad"}]}, headers=H)
    assert r2.status_code == 207
    assert len(r2.get_json()["created"]) == 1 and r2.get_json()["errors"][0]["index"] == 1
    ids = r.get_json()["created"]

    # bulk update: a real id + a missing id
    up = api.patch("/api/v1/widget/bulk",
                   json=[{"id": ids[0], "qty": 99}, {"id": 999999, "qty": 1}], headers=H)
    assert up.status_code == 207 and up.get_json()["updated"] == [ids[0]]
    assert api.get(f"/api/v1/widget/{ids[0]}", headers=H).get_json()["qty"] == 99

    # bulk delete: a real id + a missing id
    dl = api.delete("/api/v1/widget/bulk", json={"ids": [ids[1], 999999]}, headers=H)
    assert dl.status_code == 207 and dl.get_json()["deleted"] == [ids[1]]
    assert api.get(f"/api/v1/widget/{ids[1]}", headers=H).status_code == 404

    # over the cap → 400
    big = api.post("/api/v1/widget/bulk",
                   json={"records": [{"name": str(i)} for i in range(1001)]}, headers=H)
    assert big.status_code == 400

    # read-only role can't bulk-write → 403
    _new_amy(app, client)
    Hamy = _mint(app, "amy", "amy")
    _ok(client.post("/designer/permissions", data={f"access_{fid}": "read"}, follow_redirects=True))
    assert api.post("/api/v1/widget/bulk", json={"records": [{"name": "X"}]},
                    headers=Hamy).status_code == 403


def test_schema_reference_example_imports(app, client):
    """The documented reference schema (docs/schema-reference.example.json) must import
    cleanly — this pins docs/schema-json-format.md's example to the real importer."""
    from app.metadata.models import (
        Dashboard, MetaMenu, MetaRelation, MetaTable, TriggerRule, Webhook)
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


def _col(table_phys, pk, col):
    with get_engine().connect() as c:
        return c.execute(text(f"SELECT {col} FROM {table_phys} WHERE id=:i"), {"i": pk}).scalar()


def test_sla_engine(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="open\nwaiting\nresolved")
    _add_field(client, tid, "sla_state", "string", length=20)
    _add_field(client, tid, "due", "datetime")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    status_fid = _fid(app, "ticket", "status")
    state_fid = _fid(app, "ticket", "sla_state")
    due_fid = _fid(app, "ticket", "due")

    with app.app_context():
        s = SessionLocal()
        s.add(SlaPolicy(table_id=tid, name="Resolve", active=True, target_minutes=60,
                        status_field_id=status_fid, start_on_create=True,
                        pause_states="waiting", stop_states="resolved",
                        state_field_id=state_fid, due_field_id=due_fid,
                        breach_email_to="ops@example.com", breach_message="breached {title}"))
        # a second, condition-gated policy that should never start a clock here
        s.add(SlaPolicy(table_id=tid, name="VIP", active=True, target_minutes=10,
                        status_field_id=status_fid, start_on_create=True,
                        cond_field_id=status_fid, cond_op="eq", cond_value="zzz"))
        s.commit()
        pol = s.scalar(select(SlaPolicy).where(SlaPolicy.name == "Resolve"))
        vip = s.scalar(select(SlaPolicy).where(SlaPolicy.name == "VIP"))
        pol_id, vip_id = pol.id, vip.id

    # create a ticket → a running clock + write-back to sla_state/due; gated policy is skipped
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T1", "status": "open"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        clk = s.scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk and clk.state == "running" and clk.due_at is not None
        assert s.scalar(select(SlaClock).where(SlaClock.policy_id == vip_id)) is None  # gated out
    assert _col("ticket", 1, "sla_state") == "on_track"
    assert _col("ticket", 1, "due") is not None

    # move to a paused state → clock freezes, write-back flips to 'paused'
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1", "status": "waiting"}, follow_redirects=True))
    with app.app_context():
        clk = SessionLocal().scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk.state == "paused" and clk.remaining_seconds is not None
    assert _col("ticket", 1, "sla_state") == "paused"

    # resume → running again with a fresh deadline
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1", "status": "open"}, follow_redirects=True))
    with app.app_context():
        clk = SessionLocal().scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk.state == "running" and clk.due_at is not None
    assert _col("ticket", 1, "sla_state") == "on_track"

    # resolve before the deadline → met
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1", "status": "resolved"}, follow_redirects=True))
    with app.app_context():
        clk = SessionLocal().scalar(select(SlaClock).where(SlaClock.policy_id == pol_id))
        assert clk.state == "met"
    assert _col("ticket", 1, "sla_state") == "met"

    # second ticket, force its deadline into the past, then sweep → breached + escalation
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T2", "status": "open"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        clk2 = s.scalar(select(SlaClock).where(SlaClock.policy_id == pol_id,
                                               SlaClock.row_pk == "2"))
        clk2.due_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
        s.commit()
        from app import scheduler
        summary = scheduler.run_due(SessionLocal(), get_engine())
        assert summary["sla"] >= 1
        s = SessionLocal()
        clk2 = s.scalar(select(SlaClock).where(SlaClock.policy_id == pol_id,
                                               SlaClock.row_pk == "2"))
        assert clk2.state == "breached" and clk2.breach_notified
        n = s.scalar(select(Notification).where(Notification.event == "sla_breach",
                                                Notification.channel == "email"))
        assert n is not None                                  # breach escalation recorded
    assert _col("ticket", 2, "sla_state") == "breached"

    # the SLA panel renders on the record view
    _make_form_p(client, app, "ticket_view", "Ticket", tid, "view")
    vh = client.get(f"/u/view/{tid}/2").get_data(as_text=True)
    assert "SLA" in vh and "breached" in vh

    # designer pages render
    _ok(client.get("/designer/sla-policies"))
    _ok(client.get(f"/designer/sla-policies/{pol_id}"))

    # schema export/import round-trips the policy with remapped field ids
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        p = s.scalar(select(SlaPolicy).where(SlaPolicy.name == "Resolve"))
        assert p is not None
        f = s.get(MetaField, p.status_field_id)
        assert f is not None and f.phys_name == "status"      # field ref remapped, not dangling


def _login_client(app, username):
    c = app.test_client()
    _ok(c.post("/auth/login", data=dict(username=username, password="pw123456"),
               follow_redirects=True))
    return c


def _pk_of(app, table_phys, title):
    with app.app_context():
        with get_engine().connect() as c:
            return c.execute(text(f"SELECT id FROM {table_phys} WHERE title=:t"),
                             {"t": title}).scalar()


def test_approval_workflow(app, client):
    _setup(client)
    tid = _make_table(client, app, "change_req", "Change", "title")
    _add_field(client, tid, "status", "enum", enum_options="draft\nsubmitted\napproved\nrejected")
    fid = _make_form(client, app, "change_form", "Changes", tid)
    _make_form_p(client, app, "change_view", "Change", tid, "view")
    status_fid = _fid(app, "change_req", "status")

    with app.app_context():
        s = SessionLocal()
        s.add(Workflow(table_id=tid, field_id=status_fid, initial_state="draft",
                       transitions=json.dumps([
                           {"from": "draft", "to": "submitted", "roles": []},
                           {"from": "submitted", "to": "approved", "roles": []},
                           {"from": "submitted", "to": "rejected", "roles": []}])))
        s.commit()
        wf_id = s.scalar(select(Workflow.id).where(Workflow.field_id == status_fid))

    for r in ("manager", "director"):
        _ok(client.post("/designer/roles", data={"name": r, "label": r.title()},
                        follow_redirects=True))
    for u, r in (("mgr", "manager"), ("dir", "director"), ("bob", "user")):
        _ok(client.post("/auth/users/new",
                        data=dict(username=u, password="pw123456", role=r, is_active="y"),
                        follow_redirects=True))
    mgr, dir_, bob = (_login_client(app, "mgr"), _login_client(app, "dir"),
                      _login_client(app, "bob"))

    # two sequential approval steps on submitted -> approved (via the designer route)
    for pos, name, role in ((1, "Manager", "manager"), (2, "Director", "director")):
        _ok(client.post(f"/designer/approvals/{wf_id}/steps",
                        data={"from_state": "submitted", "to_state": "approved",
                              "position": str(pos), "name": name, "approver_role": role},
                        follow_redirects=True))
    _ok(client.get("/designer/approvals"))
    _ok(client.get(f"/designer/approvals/{wf_id}"))

    # bob creates + submits (draft->submitted is a direct transition)
    _ok(bob.post(f"/u/forms/{fid}/new", data={"title": "C1", "status": "draft"},
                 follow_redirects=True))
    pk = _pk_of(app, "change_req", "C1")
    _ok(bob.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "C1", "status": "submitted"},
                 follow_redirects=True))
    assert _col("change_req", pk, "status") == "submitted"

    # bob requests submitted -> approved : HELD, a pending request is created
    _ok(bob.post(f"/u/forms/{fid}/{pk}/edit", data={"title": "C1", "status": "approved"},
                 follow_redirects=True))
    assert _col("change_req", pk, "status") == "submitted"        # not moved
    with app.app_context():
        s = SessionLocal()
        req = s.scalar(select(ApprovalRequest).where(ApprovalRequest.state == "pending"))
        assert req and (req.from_state, req.to_state, req.current_position) == ("submitted", "approved", 1)
        req_id = req.id
        bob_u = s.scalar(select(AppUser).where(AppUser.username == "bob"))
        mgr_u = s.scalar(select(AppUser).where(AppUser.username == "mgr"))
        from app import approvals
        assert not approvals.can_act(s, req, bob_u)              # requester can't self-approve
        assert approvals.can_act(s, req, mgr_u)

    # the record view shows the Approvals panel (with a working Approve button for the approver)
    vh = mgr.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)
    assert "Approvals" in vh and "submitted" in vh and "approved" in vh
    assert "step 1 of 2" in vh and 'value="approve"' in vh
    vb = bob.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)
    assert "Approvals" in vb and 'value="approve"' not in vb    # requester sees status, no buttons

    # manager sees it in the inbox and approves -> advances to position 2 (still held)
    assert "approved" in mgr.get("/u/approvals").get_data(as_text=True)
    _ok(mgr.post(f"/u/approvals/{req_id}/act", data={"decision": "approve", "comment": "ok"},
                 follow_redirects=True))
    assert _col("change_req", pk, "status") == "submitted"
    with app.app_context():
        req = SessionLocal().get(ApprovalRequest, req_id)
        assert req.state == "pending" and req.current_position == 2

    # director approves -> the transition is applied for real
    _ok(dir_.post(f"/u/approvals/{req_id}/act", data={"decision": "approve", "comment": "go"},
                  follow_redirects=True))
    assert _col("change_req", pk, "status") == "approved"
    with app.app_context():
        s = SessionLocal()
        assert s.get(ApprovalRequest, req_id).state == "approved"
        n = s.scalar(select(func.count()).select_from(ApprovalAction)
                     .where(ApprovalAction.request_id == req_id))
        assert n == 2                                            # the sign-off trail

    # reject path: a second record, manager rejects -> record stays put
    _ok(bob.post(f"/u/forms/{fid}/new", data={"title": "C2", "status": "draft"},
                 follow_redirects=True))
    pk2 = _pk_of(app, "change_req", "C2")
    _ok(bob.post(f"/u/forms/{fid}/{pk2}/edit", data={"title": "C2", "status": "submitted"},
                 follow_redirects=True))
    _ok(bob.post(f"/u/forms/{fid}/{pk2}/edit", data={"title": "C2", "status": "approved"},
                 follow_redirects=True))
    with app.app_context():
        req2_id = SessionLocal().scalar(select(ApprovalRequest.id).where(
            ApprovalRequest.row_pk == str(pk2), ApprovalRequest.state == "pending"))
    _ok(mgr.post(f"/u/approvals/{req2_id}/act", data={"decision": "reject", "comment": "no"},
                 follow_redirects=True))
    assert _col("change_req", pk2, "status") == "submitted"      # not moved
    with app.app_context():
        assert SessionLocal().get(ApprovalRequest, req2_id).state == "rejected"

    # schema export/import round-trips the steps with a remapped workflow id
    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    assert len(json.loads(exp.get_data())["approval_steps"]) == 2
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        s = SessionLocal()
        new_wf = s.scalar(select(Workflow))
        assert s.scalar(select(func.count()).select_from(ApprovalStep)
                        .where(ApprovalStep.workflow_id == new_wf.id)) == 2


def test_scheduler_atomic_claim(app, client):
    """A scheduled job is claimed atomically — concurrent workers run it once."""
    from app import jobs, scheduler
    from datetime import datetime, timezone
    _setup(client)
    tid = _make_table(client, app, "tick", "Tick", "title")
    fid = _make_form(client, app, "tick_form", "Ticks", tid)
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "A"}, follow_redirects=True))
    with app.app_context():
        boss_id = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss")).id
    rid = _make_trigger(app, "tick", name="Beat", event="scheduled", schedule_minutes=60,
                        in_app=True, notify_target="user", notify_user_id=boss_id,
                        message="beat {title}")

    def _n_inapp():
        with app.app_context():
            return SessionLocal().scalar(select(func.count()).select_from(Notification)
                                         .where(Notification.channel == "in_app"))

    # run_due fires the rule once; an immediate second pass is claimed-out (no double-run)
    with app.app_context():
        assert scheduler.run_due(SessionLocal(), get_engine())["triggers"] == 1
    with app.app_context():
        assert scheduler.run_due(SessionLocal(), get_engine())["triggers"] == 0
    assert _n_inapp() == 1

    # direct claim: force due, then two claims for the same job → exactly one wins
    _force_due(app, "TriggerRule", rid)
    with app.app_context():
        s = SessionLocal()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        from app.metadata.models import TriggerRule
        assert jobs.claim_due(s, TriggerRule, rid, 60, now) is True
        assert jobs.claim_due(s, TriggerRule, rid, 60, now) is False
        assert jobs.claim_due(s, TriggerRule, rid, 0, now) is False     # disabled cadence


def test_rate_limit_shared_store(app, client):
    """The webhook rate limiter is DB-backed (shared across workers), not in-process."""
    _setup(client)
    tid = _make_table(client, app, "beep", "Beep", "label")
    _, token = _make_webhook(client, app, tid, [("label", "label")])
    assert client.post(f"/hooks/{token}", json={"label": "x"}).status_code == 201
    with app.app_context():
        n = SessionLocal().scalar(select(func.count()).select_from(RateHit))
        assert n >= 1                                                   # a hit row persisted in the DB


def test_docker_artifacts_present():
    df, dc = open("Dockerfile").read(), open("docker-compose.yml").read()
    assert "gunicorn" in df and "run:app" in df
    assert "run-jobs" in dc and "mariadb" in dc


def test_secrets_encrypted_at_rest(app, client):
    """The 5 secret columns are ciphertext in the DB but plaintext to the ORM."""
    import app.crypto as crypto
    _setup(client)
    tid = _make_table(client, app, "ci", "CI", "name")
    with app.app_context():
        s = SessionLocal()
        conn = Connection(name="peer", base_url="http://x", token="tok-SECRET")
        ds = DataSource(name="src", password="pw-SECRET")
        wh = Webhook(name="wh", target_table_id=tid, token_hash="h" * 16, prefix="pfx",
                     secret="hmac-SECRET")
        ps = PullSource(name="ps", target_table_id=tid, auth_secret="bearer-SECRET",
                        headers='{"Authorization":"Bearer XYZ"}')
        s.add_all([conn, ds, wh, ps])
        s.commit()
        ids = (conn.id, ds.id, wh.id, ps.id)

    checks = [("app_connection", "token", ids[0], "tok-SECRET"),
              ("app_data_source", "password", ids[1], "pw-SECRET"),
              ("app_webhook", "secret", ids[2], "hmac-SECRET"),
              ("app_pull_source", "auth_secret", ids[3], "bearer-SECRET"),
              ("app_pull_source", "headers", ids[3], '{"Authorization":"Bearer XYZ"}')]
    with app.app_context():
        with get_engine().connect() as c:
            for tbl, col, rid, plain in checks:
                raw = c.execute(text(f"SELECT {col} FROM {tbl} WHERE id=:i"), {"i": rid}).scalar()
                assert raw != plain                       # stored as ciphertext
                assert crypto.decrypt(raw) == plain       # which round-trips back
        s = SessionLocal()                                # ORM read returns plaintext
        assert s.get(Connection, ids[0]).token == "tok-SECRET"
        assert s.get(DataSource, ids[1]).password == "pw-SECRET"
        assert s.get(Webhook, ids[2]).secret == "hmac-SECRET"
        assert s.get(PullSource, ids[3]).auth_secret == "bearer-SECRET"
        assert s.get(PullSource, ids[3]).headers == '{"Authorization":"Bearer XYZ"}'


def test_legacy_plaintext_secret_readable(app, client):
    """A secret written before encryption (raw plaintext) still reads back via fallback."""
    _setup(client)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO app_connection (name, base_url, token, active, created_at) "
                           "VALUES ('legacy', 'http://x', 'PLAINTEXT-TOKEN', 1, NOW())"))
        s = SessionLocal()
        conn = s.scalar(select(Connection).where(Connection.name == "legacy"))
        assert conn.token == "PLAINTEXT-TOKEN"            # decrypt-with-fallback


def test_encrypt_secrets_cli(app, client):
    """`encrypt-secrets` migrates legacy plaintext rows to ciphertext."""
    import app.crypto as crypto
    _setup(client)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO app_connection (name, base_url, token, active, created_at) "
                           "VALUES ('lc', 'http://x', 'LEGACY-PLAIN', 1, NOW())"))
        with get_engine().connect() as c:
            assert c.execute(text("SELECT token FROM app_connection WHERE name='lc'")).scalar() \
                == "LEGACY-PLAIN"

    app.test_cli_runner().invoke(args=["encrypt-secrets"])

    with app.app_context():
        with get_engine().connect() as c:
            raw = c.execute(text("SELECT token FROM app_connection WHERE name='lc'")).scalar()
        assert raw != "LEGACY-PLAIN" and crypto.decrypt(raw) == "LEGACY-PLAIN"
        s = SessionLocal()
        assert s.scalar(select(Connection).where(Connection.name == "lc")).token == "LEGACY-PLAIN"


def test_totp_roundtrip():
    from app import totp
    s = totp.new_secret()
    assert totp.verify(s, totp.now_code(s)) is True
    assert totp.verify(s, "000000") is False or totp.now_code(s) == "000000"
    # window tolerance: the previous 30s step still verifies
    import time as _t
    assert totp.verify(s, totp.now_code(s, at=_t.time() - 30)) is True
    plain, hashed = totp.make_backup_codes(3)
    ok, rest = totp.consume_backup_code(hashed, plain[0])
    assert ok and totp.backup_count(rest) == 2 and totp.consume_backup_code(rest, plain[0])[0] is False


def _enroll_mfa(app, client):
    """Enable MFA for the logged-in user; return (secret, backup_codes)."""
    from app import totp
    page = client.get("/auth/mfa").get_data(as_text=True)
    secret = re.search(r"secret=([A-Z2-7]+)", page).group(1)
    resp = client.post("/auth/mfa", data={"action": "enable", "code": totp.now_code(secret)},
                       follow_redirects=True).get_data(as_text=True)
    backups = re.findall(r"[0-9a-f]{4}-[0-9a-f]{4}", resp)
    return secret, backups


def test_mfa_enroll_and_two_step_login(app, client):
    from app import totp
    _setup(client)                                   # logged in as 'boss'
    secret, backups = _enroll_mfa(app, client)
    assert len(backups) == 10
    with app.app_context():
        u = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss"))
        assert u.mfa_enabled and u.totp_secret == secret
        with get_engine().connect() as c:
            raw = c.execute(text("SELECT totp_secret FROM app_user WHERE id=:i"), {"i": u.id}).scalar()
        assert raw != secret                         # secret is encrypted at rest

    client.get("/auth/logout")
    # password alone redirects to the second factor and does NOT authenticate
    r = client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    assert r.status_code == 302 and "/auth/mfa-verify" in r.headers["Location"]
    assert client.get("/auth/account").status_code == 302   # still not logged in

    # a wrong code is rejected; the right code logs in
    client.post("/auth/mfa-verify", data={"code": "000001"})
    assert client.get("/auth/account").status_code == 302
    r = client.post("/auth/mfa-verify", data={"code": totp.now_code(secret)})
    assert r.status_code == 302
    assert client.get("/auth/account").status_code == 200   # authenticated now

    # a backup code also works (and is single-use)
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    r = client.post("/auth/mfa-verify", data={"code": backups[0]})
    assert r.status_code == 302 and client.get("/auth/account").status_code == 200
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    client.post("/auth/mfa-verify", data={"code": backups[0]})   # already spent
    assert client.get("/auth/account").status_code == 302       # not accepted twice


def test_mfa_admin_reset(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        amy = s.scalar(select(AppUser).where(AppUser.username == "amy"))
        amy.mfa_enabled, amy.totp_secret = True, "JBSWY3DPEHPK3PXP"
        s.commit()
        amy_id = amy.id
    _ok(client.post(f"/auth/users/{amy_id}/reset-mfa", follow_redirects=True))
    with app.app_context():
        amy = SessionLocal().get(AppUser, amy_id)
        assert not amy.mfa_enabled and amy.totp_secret is None


def test_require_mfa_enforcement(app, client):
    _setup(client)                                   # boss logged in, no MFA
    app.config["REQUIRE_MFA"] = True
    try:
        r = client.get("/designer/dashboard")
        assert r.status_code == 302 and "/auth/mfa" in r.headers["Location"]
        _ok(client.get("/auth/mfa"))                 # the enroll page itself is reachable
    finally:
        app.config["REQUIRE_MFA"] = False


# --------------------------------------------------------------------------- #
# SSO (OIDC) — stub IdP: real RSA-signed tokens verified against a served JWKS
# --------------------------------------------------------------------------- #
def _b64u(b):
    import base64
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _oidc_setup(app):
    from cryptography.hazmat.primitives.asymmetric import rsa
    import app.oidc as oidc
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    holder = {"id_token": None}
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
                      "n": _b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
                      "e": _b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))}]}
    disco = {"issuer": "https://idp.test", "authorization_endpoint": "https://idp.test/auth",
             "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/jwks"}

    def transport(method, url, headers, body):
        if url.endswith("openid-configuration"):
            return 200, json.dumps(disco)
        if url.endswith("/jwks"):
            return 200, json.dumps(jwks)
        if url.endswith("/token"):
            return 200, json.dumps({"id_token": holder["id_token"], "access_token": "a"})
        return 404, "{}"

    oidc.set_transport(transport)
    oidc.reset_caches()
    app.config.update(OIDC_ISSUER="https://idp.test", OIDC_CLIENT_ID="cid",
                      OIDC_CLIENT_SECRET="sec", OIDC_ENABLED=True, OIDC_PROVISION="link")
    return key, holder


def _oidc_teardown(app):
    import app.oidc as oidc
    oidc.set_transport(None)
    oidc.reset_caches()
    app.config.update(OIDC_ENABLED=False, OIDC_PROVISION="link")


def _sign(key, claims, alg="RS256", kid="k1"):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    head = _b64u(json.dumps({"alg": alg, "kid": kid}).encode())
    payload = _b64u(json.dumps(claims).encode())
    sig = key.sign((head + "." + payload).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64u(sig)}"


def _sso_flow(client, key, holder, claims):
    """Drive /auth/oidc/login → callback; the token carries the app-issued nonce."""
    import re as _re
    import time as _t
    loc = client.get("/auth/oidc/login").headers["Location"]
    state = _re.search(r"state=([^&]+)", loc).group(1)
    nonce = _re.search(r"nonce=([^&]+)", loc).group(1)
    full = {"iss": "https://idp.test", "aud": "cid", "sub": "sub-1",
            "exp": int(_t.time()) + 300, "iat": int(_t.time()), "nonce": nonce}
    full.update(claims)
    holder["id_token"] = _sign(key, full)
    return client.get(f"/auth/oidc/callback?code=x&state={state}")


def test_oidc_login_links_existing(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="alice@acme.com", password="pw123456", role="user",
                              is_active="y"), follow_redirects=True))
    client.get("/auth/logout")
    key, holder = _oidc_setup(app)
    try:
        assert "oidc/login" in client.get("/auth/login").get_data(as_text=True)  # SSO button
        r = _sso_flow(client, key, holder, {"email": "alice@acme.com", "sub": "alice-sub"})
        assert r.status_code == 302
        assert client.get("/auth/account").status_code == 200          # logged in
        with app.app_context():
            u = SessionLocal().scalar(select(AppUser).where(AppUser.username == "alice@acme.com"))
            assert u.oidc_subject == "alice-sub"                       # linked for next time
    finally:
        _oidc_teardown(app)


def test_oidc_refuses_unknown_in_link_mode(app, client):
    _setup(client)
    client.get("/auth/logout")
    key, holder = _oidc_setup(app)
    try:
        r = _sso_flow(client, key, holder, {"email": "nobody@acme.com", "sub": "x"})
        assert r.status_code == 302 and "/auth/login" in r.headers["Location"]
        assert client.get("/auth/account").status_code == 302         # not authenticated
        with app.app_context():
            assert SessionLocal().scalar(
                select(AppUser).where(AppUser.username == "nobody@acme.com")) is None
    finally:
        _oidc_teardown(app)


def test_oidc_jit_provision(app, client):
    _setup(client)
    client.get("/auth/logout")
    key, holder = _oidc_setup(app)
    app.config["OIDC_PROVISION"] = "jit"
    try:
        r = _sso_flow(client, key, holder, {"email": "newbie@acme.com", "sub": "new-sub"})
        assert r.status_code == 302 and client.get("/auth/account").status_code == 200
        with app.app_context():
            u = SessionLocal().scalar(select(AppUser).where(AppUser.username == "newbie@acme.com"))
            assert u and u.role == "user" and u.oidc_subject == "new-sub"
    finally:
        _oidc_teardown(app)


def test_oidc_rejects_bad_token(app, client):
    import time as _t
    _setup(client)
    key, holder = _oidc_setup(app)
    try:
        import app.oidc as oidc
        with app.app_context():
            base = {"iss": "https://idp.test", "aud": "cid", "sub": "s", "email": "e@x.y",
                    "exp": int(_t.time()) + 300, "iat": int(_t.time()), "nonce": "N"}
            assert oidc.verify_id_token(_sign(key, base), "N")["sub"] == "s"   # good token

            def _rejected(token, nonce="N"):
                try:
                    oidc.verify_id_token(token, nonce)
                    return False
                except oidc.OidcError:
                    return True

            assert _rejected(_sign(key, dict(base, aud="other")))             # wrong audience
            assert _rejected(_sign(key, dict(base, exp=int(_t.time()) - 100)))  # expired
            assert _rejected(_sign(key, base), nonce="DIFFERENT")             # nonce mismatch
            assert _rejected(_sign(key, dict(base, iss="https://evil.test"))) # issuer mismatch
            tok = _sign(key, base)                                            # tampered signature
            h, p, s = tok.split(".")
            assert _rejected(f"{h}.{p}.{('a' if s[0] != 'a' else 'b') + s[1:]}")
            none_tok = _b64u(json.dumps({"alg": "none"}).encode()) + "." \
                + _b64u(json.dumps(base).encode()) + "."
            assert _rejected(none_tok)                                        # alg 'none' refused

        # state mismatch is refused at the route level
        client.get("/auth/oidc/login")
        r = client.get("/auth/oidc/callback?code=x&state=WRONGSTATE")
        assert r.status_code == 302 and "/auth/login" in r.headers["Location"]
    finally:
        _oidc_teardown(app)


def test_bulk_user_import(app, client):
    _setup(client)
    rows = "alice,user,pw123456\nbob,designer\n# a comment\nboss,user\ncarol,nosuchrole"
    res = client.post("/auth/users/bulk", data={"rows": rows},
                      follow_redirects=True).get_data(as_text=True)
    assert "2 created" in res and "1 skipped" in res and "1 error" in res   # boss exists; bad role
    with app.app_context():
        s = SessionLocal()
        alice = s.scalar(select(AppUser).where(AppUser.username == "alice"))
        bob = s.scalar(select(AppUser).where(AppUser.username == "bob"))
        assert alice.role == "user" and alice.check_password("pw123456")
        assert bob.role == "designer" and not bob.check_password("")        # SSO-only / unusable pw
        assert s.scalar(select(AppUser).where(AppUser.username == "carol")) is None
