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
