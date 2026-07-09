"""Ops batch: people email, bulk edit, maintenance windows, recurring records."""
from sqlalchemy import select

from app import mailer
from app.db import SessionLocal
from app.metadata.models import AppUser
from tests.helpers import (
    _add_field,
    _make_form,
    _make_form_p,
    _make_table,
    _new_amy,
    _ok,
    _setup,
)


def test_email_notifications(app, client):
    _setup(client)
    tid = _make_table(client, app, "mtask", "M task", "title")
    _add_field(client, tid, "assignee", "user")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "mtask_form", "M tasks", tid)
    _make_form_p(client, app, "mtask_view", "M task", tid, "view")
    amy = _new_amy(app, client)
    _ok(amy.post("/auth/account/contact",
                 data={"email": "amy@example.com", "notify_email": "y"},
                 follow_redirects=True))
    with app.app_context():
        amy_id = SessionLocal().scalar(
            select(AppUser).where(AppUser.username == "amy")).id
    mailer.OUTBOX.clear()

    # assignment on create → amy gets an email
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T1", "assignee": str(amy_id)},
                    follow_redirects=True))
    assert any(to == "amy@example.com" and "assigned to you" in subj
               for to, subj, _ in mailer.OUTBOX)
    mailer.OUTBOX.clear()

    # watcher gets comment + update emails
    _ok(amy.post(f"/u/watch/{tid}/1", follow_redirects=True))
    _ok(client.post(f"/u/comments/{tid}/1",
                    data={"body": "checking the uplink", "visibility": "public"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "T1b", "assignee": str(amy_id)},
                    follow_redirects=True))
    bodies = " | ".join(b for _, _, b in mailer.OUTBOX)
    assert "checking the uplink" in bodies         # comment email
    assert "changed:" in bodies                    # watch email
    mailer.OUTBOX.clear()

    # opting out silences everything for amy
    _ok(amy.post("/auth/account/contact", data={"email": "amy@example.com"},
                 follow_redirects=True))            # checkbox unticked
    _ok(client.post(f"/u/comments/{tid}/1",
                    data={"body": "more news", "visibility": "public"},
                    follow_redirects=True))
    assert not any(to == "amy@example.com" for to, _, _ in mailer.OUTBOX)


def test_bulk_edit(app, client):
    from sqlalchemy import text

    from app.db import get_engine
    from tests.helpers import _make_workflow, _status_field_id
    _setup(client)
    tid = _make_table(client, app, "btask", "B task", "name")
    _add_field(client, tid, "status", "enum", enum_options="new\ndone")
    fid = _make_form(client, app, "btask_form", "B tasks", tid)
    for n in ("r1", "r2", "r3"):
        _ok(client.post(f"/u/forms/{fid}/new", data={"name": n, "status": "new"},
                        follow_redirects=True))

    # the list offers Edit selected → confirm page shows the selection
    lst = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert "Edit selected" in lst
    page = client.post(f"/u/forms/{fid}/bulk-edit",
                       data={"ids": ["1", "2"]}).get_data(as_text=True)
    assert "2 record(s) selected" in page and 'value="status"' in page

    # apply: rows 1+2 → done, row 3 untouched
    _ok(client.post(f"/u/forms/{fid}/bulk-edit/apply",
                    data={"ids": ["1", "2"], "column": "status", "value": "done"},
                    follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            rows = dict(c.execute(text("SELECT id, status FROM btask")).all())
    assert rows == {1: "done", 2: "done", 3: "new"}

    # an invalid value is rejected up front — nothing changes
    r = client.post(f"/u/forms/{fid}/bulk-edit/apply",
                    data={"ids": ["3"], "column": "status", "value": "nonsense"},
                    follow_redirects=True)
    assert "Updated" not in r.get_data(as_text=True)
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT status FROM btask WHERE id=3")).scalar() == "new"

    # with a workflow, illegal moves are skipped per row (done rows have no
    # edge back to new; row 3 may move new→done)
    _make_workflow(client, app, _status_field_id(app, "btask"),
                   [{"from": "new", "to": "done", "roles": []}], "new")
    r = client.post(f"/u/forms/{fid}/bulk-edit/apply",
                    data={"ids": ["1", "3"], "column": "status", "value": "new"},
                    follow_redirects=True)
    assert "skipped" in r.get_data(as_text=True)
    with app.app_context():
        with get_engine().connect() as c:
            rows = dict(c.execute(text("SELECT id, status FROM btask")).all())
    assert rows[1] == "done" and rows[3] == "new"   # 1 blocked; 3 unchanged-skip


def test_maintenance_windows(app, client):
    from datetime import datetime, timedelta, timezone

    from app import sla
    from app.metadata.models import Notification, SlaClock, SlaPolicy
    from tests.helpers import _make_trigger
    from tests.test_noc_tools import _field_id
    _setup(client)

    # a change record to link the window to
    chg_tid = _make_table(client, app, "chg", "Change", "title")
    chg_fid = _make_form(client, app, "chg_form", "Changes", chg_tid)
    _make_form_p(client, app, "chg_view", "Change", chg_tid, "view")
    _ok(client.post(f"/u/forms/{chg_fid}/new", data={"title": "Upgrade core"},
                    follow_redirects=True))

    # a service table with SLA + in-app trigger + a watcher
    tid = _make_table(client, app, "svc", "Service", "name")
    _add_field(client, tid, "status", "enum", enum_options="up\ndown")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "svc_form", "Services", tid)
    _make_form_p(client, app, "svc_view", "Service", tid, "view")
    _make_trigger(app, "svc", event="update", in_app=True, notify_target="actor")
    with app.app_context():
        s = SessionLocal()
        s.add(SlaPolicy(table_id=tid, name="Fix", active=True, target_minutes=60,
                        status_field_id=_field_id(app, "svc", "status"),
                        start_on_create=True, stop_states="up"))
        s.commit()
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "dns", "status": "down"},
                    follow_redirects=True))
    amy = _new_amy(app, client)
    _ok(amy.post(f"/u/watch/{tid}/1", follow_redirects=True))

    # schedule an active window scoped to svc, linked to the change record
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    fmt = "%Y-%m-%dT%H:%M"
    _ok(client.post("/designer/maintenance", data={
        "name": "Core upgrade", "starts_at": (now - timedelta(hours=1)).strftime(fmt),
        "ends_at": (now + timedelta(hours=1)).strftime(fmt),
        "table_id": str(tid), "record_table_id": str(chg_tid), "record_pk": "1",
    }, follow_redirects=True))
    page = client.get("/designer/maintenance").get_data(as_text=True)
    assert "Core upgrade" in page and ">active<" in page

    # banner on the scoped list + the window shown on the linked change record
    assert "alerts are held" in client.get(f"/u/forms/{fid}").get_data(as_text=True)
    chg_view = client.get(f"/u/view/{chg_tid}/1").get_data(as_text=True)
    assert "Core upgrade" in chg_view and "Maintenance" in chg_view

    # watch + trigger notifications are held during the window
    _ok(client.post(f"/u/forms/{fid}/1/edit", data={"name": "dns", "status": "down"},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        assert s.scalar(select(Notification).where(
            Notification.event == "watch")) is None
        held = s.scalar(select(Notification).where(Notification.channel == "in_app",
                                                   Notification.status == "skipped"))
        assert held is not None and "maintenance" in (held.detail or "")

    # SLA breach is held while the window is active, fires after it's removed
    with app.app_context():
        s = SessionLocal()
        clk = s.scalar(select(SlaClock))
        clk.due_at = now - timedelta(minutes=5)
        s.commit()
        sla.run_breach_sweep(s)
        assert s.scalar(select(SlaClock)).state == "running"     # held
        wid_page = client.get("/designer/maintenance").get_data(as_text=True)
    import re as _re
    wid = _re.search(r"/designer/maintenance/(\d+)/delete", wid_page).group(1)
    _ok(client.post(f"/designer/maintenance/{wid}/delete", follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        sla.run_breach_sweep(s)
        assert s.scalar(select(SlaClock)).state == "breached"    # fires afterwards

    # a bad linked record is rejected
    client.post("/designer/maintenance", data={
        "name": "Bad", "starts_at": now.strftime(fmt),
        "ends_at": (now + timedelta(hours=1)).strftime(fmt),
        "record_table_id": str(chg_tid), "record_pk": "999",
    }, follow_redirects=True)
    assert "Bad" not in client.get("/designer/maintenance").get_data(as_text=True)
