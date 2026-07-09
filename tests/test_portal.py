"""Record conversations (staff ⇄ customer comments) and the customer portal."""
from sqlalchemy import select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import AppUser, Comment, Notification
from tests.helpers import (
    _add_field,
    _make_form,
    _make_form_p,
    _make_table,
    _make_workflow,
    _new_amy,
    _ok,
    _setup,
    _status_field_id,
)


def _new_portal_user(app, client, username):
    """Create a portal-role account via the Users page and return its client."""
    _ok(client.post("/auth/users/new",
                    data=dict(username=username, password="pw123456", role="portal",
                              is_active="y"), follow_redirects=True))
    c = app.test_client()
    _ok(c.post("/auth/login", data=dict(username=username, password="pw123456"),
               follow_redirects=True))
    return c


def test_conversation_on_record_view(app, client):
    _setup(client)
    tid = _make_table(client, app, "issue", "Issue", "title")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "issue_form", "Issues", tid)
    _make_form_p(client, app, "issue_view", "Issue", tid, "view")
    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "Link down"},
                    follow_redirects=True))

    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "Conversation" in view and "No comments yet" in view

    _ok(client.post(f"/u/comments/{tid}/1",
                    data={"body": "Checking the switch", "visibility": "internal"},
                    follow_redirects=True))
    _ok(client.post(f"/u/comments/{tid}/1",
                    data={"body": "We are on it", "visibility": "public"},
                    follow_redirects=True))
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "Checking the switch" in view and "internal note" in view
    assert "We are on it" in view

    # an empty body is rejected, not stored
    client.post(f"/u/comments/{tid}/1", data={"body": "  ", "visibility": "public"},
                follow_redirects=True)
    with app.app_context():
        assert SessionLocal().scalar(
            select(Comment).where(Comment.body == "")) is None

    # a second staff user replies → the prior participant (boss) is notified
    amy = _new_amy(app, client)
    _ok(amy.post(f"/u/comments/{tid}/1",
                 data={"body": "Fiber team dispatched", "visibility": "public"},
                 follow_redirects=True))
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "Fiber team dispatched" in view and "amy" in view
    with app.app_context():
        s = SessionLocal()
        boss_id = s.scalar(select(AppUser).where(AppUser.username == "boss")).id
        n = s.scalar(select(Notification).where(Notification.event == "comment",
                                                Notification.user_id == boss_id))
        assert n is not None and "Fiber team dispatched" in n.body


def test_portal_mode(app, client):
    _setup(client)
    # one catalog form on an owner-stamped table; one on a table without stamps
    tid = _make_table(client, app, "incident", "Incident", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\nsolved")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "incident_form", "Report an incident", tid)
    _make_form_p(client, app, "incident_view", "Incident", tid, "view")
    _ok(client.post(f"/designer/forms/{fid}/catalog",
                    data={"in_catalog": "y", "catalog_group": "Network"},
                    follow_redirects=True))
    t2 = _make_table(client, app, "feedbk", "Feedbk", "name")
    f2 = _make_form(client, app, "feedbk_form", "Feedbk", t2)
    _ok(client.post(f"/designer/forms/{f2}/catalog", data={"in_catalog": "y"},
                    follow_redirects=True))

    carl = _new_portal_user(app, client, "carl")

    # routing: portal users land on /portal; /u and /designer are blocked
    assert "/portal" in carl.get("/", follow_redirects=False).headers["Location"]
    r = carl.get("/u/", follow_redirects=False)
    assert r.status_code in (301, 302) and "/portal" in r.headers["Location"]
    assert carl.get("/designer/", follow_redirects=False).status_code in (302, 403)

    # home offers only the owner-stamped catalog form
    home = carl.get("/portal/").get_data(as_text=True)
    assert "Report an incident" in home and "Network" in home
    assert "Feedbk" not in home
    assert carl.get(f"/portal/new/{f2}").status_code == 404

    # submit a ticket → lands on its ticket page; created_by is the customer
    r = carl.post(f"/portal/new/{fid}", data={"title": "Fiber cut", "status": "new"},
                  follow_redirects=True)
    _ok(r)
    assert "Fiber cut" in r.get_data(as_text=True)
    with app.app_context():
        s = SessionLocal()
        carl_id = s.scalar(select(AppUser).where(AppUser.username == "carl")).id
        with get_engine().connect() as c:
            row = c.execute(text("SELECT id, created_by FROM incident")).mappings().first()
        assert row["created_by"] == carl_id
        pk = row["id"]

    home = carl.get("/portal/").get_data(as_text=True)
    assert "Fiber cut" in home and 'class="chip' in home    # listed with status chip

    # another customer cannot open it
    dana = _new_portal_user(app, client, "dana")
    assert dana.get(f"/portal/ticket/{tid}/{pk}").status_code == 404

    # staff: internal note stays hidden, public reply shows + notifies the customer
    _ok(client.post(f"/u/comments/{tid}/{pk}",
                    data={"body": "Secret diagnostics", "visibility": "internal"},
                    follow_redirects=True))
    _ok(client.post(f"/u/comments/{tid}/{pk}",
                    data={"body": "Crew dispatched", "visibility": "public"},
                    follow_redirects=True))
    ticket = carl.get(f"/portal/ticket/{tid}/{pk}").get_data(as_text=True)
    assert "Crew dispatched" in ticket and "Secret diagnostics" not in ticket
    home = carl.get("/portal/").get_data(as_text=True)
    assert 'class="badge"' in home                          # unread bell count
    notif = carl.get("/portal/notifications").get_data(as_text=True)
    assert "Crew dispatched" in notif and f"/portal/ticket/{tid}/{pk}" in notif

    # customer reply → staff participant notified, visible on the record view
    _ok(carl.post(f"/portal/ticket/{tid}/{pk}/comment",
                  data={"body": "Any update?"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        boss_id = s.scalar(select(AppUser).where(AppUser.username == "boss")).id
        n = s.scalar(select(Notification).where(Notification.user_id == boss_id,
                                                Notification.event == "comment"))
        assert n is not None and "Any update?" in n.body
    assert "Any update?" in client.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)

    # mark-all-read clears the portal badge
    _ok(carl.post("/portal/notifications", follow_redirects=True))
    assert 'class="badge"' not in carl.get("/portal/").get_data(as_text=True)


def test_portal_close_ticket(app, client):
    _setup(client)
    tid = _make_table(client, app, "ncase", "Net case", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\nack\nsolved")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "ncase_form", "Report a net case", tid)
    _make_form_p(client, app, "ncase_view", "Net case", tid, "view")
    # publish + allow closing into "solved" from the designer catalog page
    _ok(client.post("/designer/catalog", data={f"in_{fid}": "y", f"group_{fid}": "",
                                               f"desc_{fid}": "", f"close_{fid}": "solved"},
                    follow_redirects=True))

    carl = _new_portal_user(app, client, "carl")
    _ok(carl.post(f"/portal/new/{fid}", data={"title": "Latency spike", "status": "new"},
                  follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            pk = c.execute(text("SELECT id FROM ncase")).scalar()

    # the button is offered; closing writes the status + a public comment
    page = carl.get(f"/portal/ticket/{tid}/{pk}").get_data(as_text=True)
    assert "Close this ticket" in page
    _ok(carl.post(f"/portal/ticket/{tid}/{pk}/close",
                  data={"reason": "works again since 10:30"}, follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT status FROM ncase WHERE id=:i"),
                             {"i": pk}).scalar() == "solved"
    page = carl.get(f"/portal/ticket/{tid}/{pk}").get_data(as_text=True)
    assert "Closed by customer: works again since 10:30" in page
    assert "Close this ticket" not in page                  # already closed
    # staff sees the closure in the thread + audit history exists
    staff = client.get(f"/u/view/{tid}/{pk}").get_data(as_text=True)
    assert "Closed by customer" in staff

    # with a workflow lacking the current→close edge, the button disappears
    # and a direct POST is refused
    _make_workflow(client, app, _status_field_id(app, "ncase"),
                   [{"from": "new", "to": "ack", "roles": []}], "new")
    _ok(carl.post(f"/portal/new/{fid}", data={"title": "Port flap", "status": "new"},
                  follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            pk2 = c.execute(text("SELECT MAX(id) FROM ncase")).scalar()
    page = carl.get(f"/portal/ticket/{tid}/{pk2}").get_data(as_text=True)
    assert "Close this ticket" not in page
    carl.post(f"/portal/ticket/{tid}/{pk2}/close", data={}, follow_redirects=True)
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT status FROM ncase WHERE id=:i"),
                             {"i": pk2}).scalar() == "new"   # unchanged


def _mk_company(client, app, name, parent_id=""):
    from app.metadata.models import Company
    _ok(client.post("/designer/companies", data={"name": name, "parent_id": parent_id},
                    follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(Company).where(Company.name == name)).id


def _mk_user(client, app, username, role, company_id=""):
    _ok(client.post("/auth/users/new",
                    data=dict(username=username, password="pw123456", role=role,
                              is_active="y", company_id=company_id),
                    follow_redirects=True))
    c = app.test_client()
    _ok(c.post("/auth/login", data=dict(username=username, password="pw123456"),
               follow_redirects=True))
    return c


def test_portal_org_visibility(app, client):
    _setup(client)
    tid = _make_table(client, app, "oreq", "Org req", "title")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "oreq_form", "Org request", tid)
    _make_form_p(client, app, "oreq_view", "Org req", tid, "view")
    _ok(client.post(f"/designer/forms/{fid}/catalog", data={"in_catalog": "y"},
                    follow_redirects=True))

    # a company chain: HoldCo ─ Acme; Globex stands alone
    hold_id = _mk_company(client, app, "HoldCo")
    acme_id = _mk_company(client, app, "Acme", hold_id)
    glob_id = _mk_company(client, app, "Globex")

    ann = _mk_user(client, app, "ann", "portal", acme_id)
    ben = _mk_user(client, app, "ben", "portal", acme_id)
    gil = _mk_user(client, app, "gil", "portal", glob_id)
    hq = _mk_user(client, app, "hq", "portal", hold_id)

    _ok(ann.post(f"/portal/new/{fid}", data={"title": "VPN down"},
                 follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            pk = c.execute(text("SELECT id FROM oreq")).scalar()

    # the Acme colleague sees, opens, and comments on the ticket
    home = ben.get("/portal/").get_data(as_text=True)
    assert "Acme tickets" in home and "VPN down" in home and ">ann<" in home
    ticket = ben.get(f"/portal/ticket/{tid}/{pk}").get_data(as_text=True)
    assert "VPN down" in ticket and "by ann" in ticket
    _ok(ben.post(f"/portal/ticket/{tid}/{pk}/comment",
                 data={"body": "affects our whole office"}, follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        ann_id = s.scalar(select(AppUser).where(AppUser.username == "ann")).id
        n = s.scalar(select(Notification).where(Notification.user_id == ann_id,
                                                Notification.event == "comment"))
        assert n is not None                      # the creator heard about it

    # the parent-company account sees the whole chain below it
    assert "VPN down" in hq.get("/portal/").get_data(as_text=True)
    _ok(hq.get(f"/portal/ticket/{tid}/{pk}"))

    # another company is walled off completely — and can't see upward either
    assert gil.get(f"/portal/ticket/{tid}/{pk}").status_code == 404
    assert "VPN down" not in gil.get("/portal/").get_data(as_text=True)
    _ok(gil.post(f"/portal/new/{fid}", data={"title": "Globex issue"},
                 follow_redirects=True))
    assert "Globex issue" not in ann.get("/portal/").get_data(as_text=True)

    # a *staff* account in the same company never widens the portal scope
    stan = _mk_user(client, app, "stan", "user", acme_id)
    _ok(stan.post(f"/u/forms/{fid}/new", data={"title": "internal-only item"},
                  follow_redirects=True))
    assert "internal-only item" not in ben.get("/portal/").get_data(as_text=True)
