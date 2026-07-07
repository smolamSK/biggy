"""Status workflows, trigger rules and help pages. (Split from test_features.py.)"""
import io
import json
import re
from datetime import date

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    AppUser,
    Notification,
)
from tests.helpers import (
    _add_field,
    _fid,
    _make_form,
    _make_table,
    _make_trigger,
    _make_workflow,
    _mint,
    _new_amy,
    _ok,
    _setup,
    _status_field_id,
)


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

def test_triggers_fire_actions(app, client):
    _setup(client)
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\ntriaged\nresolved")
    _add_field(client, tid, "resolved_on", "date")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    status_fid = _status_field_id(app)
    resolved_fid = _fid(app, "ticket", "resolved_on")
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
    home = client.get("/u/").get_data(as_text=True)
    assert 'id="badge-notif"' in home and 'class="badge">1</span>' in home  # topbar bell + count
    _ok(client.post("/u/notifications/read", follow_redirects=True))
    with app.app_context():
        n = SessionLocal().scalar(select(Notification).where(Notification.channel == "in_app"))
        assert n.status == "read"

def test_trigger_event_scoping(app, client):
    _setup(client)
    tid = _make_table(client, app, "thing", "Thing", "name")
    _add_field(client, tid, "status", "enum", enum_options="a\nb")
    fid = _make_form(client, app, "thing_form", "Things", tid)
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

def test_help_pages(app, client):
    _setup(client)                                      # boss = designer, logged in

    # /help redirects to the user manual
    r = client.get("/help")
    assert r.status_code in (301, 302) and "/help/user" in r.headers["Location"]

    # both manuals render for a designer; the topbar exposes the Help link
    u = client.get("/help/user")
    _ok(u)
    ub = u.get_data(as_text=True)
    assert "User Manual" in ub and 'href="/help/user"' in ub  # help link in the account menu
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


def test_trigger_create_record_and_depth_guard(app, client):
    """The create-record action makes a templated record; chains are depth-capped."""
    _setup(client)
    inc_tid = _make_table(client, app, "incident", "Incident", "title")
    _add_field(client, inc_tid, "severity", "enum", enum_options="sev1\nsev2")
    inc_fid = _make_form(client, app, "incident_form", "Incidents", inc_tid)
    cr_tid = _make_table(client, app, "change_req", "Change", "title")

    _make_trigger(app, "incident", name="Auto-CR", event="create",
                  cond_field_id=_fid(app, "incident", "severity"), cond_op="eq",
                  cond_value="sev1", create_table_id=cr_tid,
                  create_field_map=json.dumps([{"target": "title",
                                                "source": "CR for {title}"}]))

    _ok(client.post(f"/u/forms/{inc_fid}/new", data={"title": "Minor", "severity": "sev2"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{inc_fid}/new", data={"title": "Outage", "severity": "sev1"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            titles = [r[0] for r in c.execute(text("SELECT title FROM change_req")).all()]
    assert titles == ["CR for Outage"]                 # only the sev1 incident spawned a CR

    # a self-referential chain is capped at depth 3 (1 manual + 3 chained)
    loop_tid = _make_table(client, app, "loopy", "Loopy", "title")
    loop_fid = _make_form(client, app, "loopy_form", "Loopies", loop_tid)
    _make_trigger(app, "loopy", name="Self-spawn", event="create",
                  create_table_id=loop_tid,
                  create_field_map=json.dumps([{"target": "title",
                                                "source": "again {title}"}]))
    _ok(client.post(f"/u/forms/{loop_fid}/new", data={"title": "seed"},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        with get_engine().connect() as c:
            n = c.execute(text("SELECT COUNT(*) FROM loopy")).scalar()
        assert n == 4                                  # no runaway loop
        capped = s.scalar(select(Notification).where(
            Notification.channel == "error", Notification.status == "skipped"))
        assert capped is not None and "depth cap" in capped.detail


def test_trigger_webhook_text_payload(app, client):
    """webhook_format='text' posts {"text": message} — the Slack/Teams shape."""
    from app.metadata.models import MetaTable, TriggerRule
    from app.triggers import _webhook_payload
    _setup(client)
    mt = MetaTable(phys_name="thing", label="Thing")
    row = {"id": 7, "name": "Router-9"}
    text_rule = TriggerRule(table_id=1, name="r", event="update",
                            webhook_url="http://chat", webhook_format="text",
                            message="Alert: {name}")
    with app.app_context():
        assert _webhook_payload(text_rule, "update", mt, row, None) == \
            {"text": "Alert: Router-9"}
        json_rule = TriggerRule(table_id=1, name="r", event="update",
                                webhook_url="http://x", webhook_format="json")
        p = _webhook_payload(json_rule, "update", mt, row, {"id": 7, "name": "Old"})
        assert p["event"] == "update" and p["table"] == "thing"
        assert p["record"]["name"] == "Router-9" and p["old"]["name"] == "Old"
