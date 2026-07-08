"""NOC/staff ergonomics: assignee (user field), SLA in lists, watch, activity."""
from sqlalchemy import select, text

from app.db import SessionLocal, get_engine
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


def test_user_field_assign_and_my_work(app, client):
    _setup(client)
    tid = _make_table(client, app, "tick", "Tick", "title")
    _add_field(client, tid, "assignee", "user")
    _add_field(client, tid, "status", "enum", enum_options="open\ndone")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "tick_form", "Ticks", tid)
    _make_form_p(client, app, "tick_view", "Tick", tid, "view")
    amy = _new_amy(app, client)
    with app.app_context():
        s = SessionLocal()
        boss_id = s.scalar(select(AppUser).where(AppUser.username == "boss")).id
        amy_id = s.scalar(select(AppUser).where(AppUser.username == "amy")).id

    # the form renders a user picker; boss files a ticket assigned to amy
    page = client.get(f"/u/forms/{fid}/new").get_data(as_text=True)
    assert 'name="assignee"' in page and ">amy<" in page
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "T1", "status": "open", "assignee": str(amy_id)},
                    follow_redirects=True))

    # record page shows the username, not the raw id
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "amy" in view

    # the "me" filter resolves per viewer — the same saved view works for both
    q = {"fcol": "assignee", "fop": "eq", "fval": "me"}
    assert "T1" in amy.get(f"/u/forms/{fid}", query_string=q).get_data(as_text=True)
    assert "No matching records" in client.get(f"/u/forms/{fid}",
                                               query_string=q).get_data(as_text=True)

    # My work panel on amy's home lists the assignment
    home = amy.get("/u/").get_data(as_text=True)
    assert "My work" in home and "T1" in home

    # boss takes the ticket over with one click
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "Assign to me" in view
    _ok(client.post(f"/u/assign/{tid}/1", follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT assignee FROM tick")).scalar() == boss_id
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "Assign to me" not in view and "boss" in view

    # default token: 'me' on a user field assigns the creator
    _ok(client.post(f"/designer/tables/{tid}/fields/"
                    f"{_field_id(app, 'tick', 'assignee')}/edit",
                    data={"phys_name": "assignee", "label": "Assignee",
                          "data_type": "user", "nullable": "y", "default_value": "me"},
                    follow_redirects=True))
    _ok(amy.post(f"/u/forms/{fid}/new", data={"title": "T2", "status": "open",
                                              "assignee": ""}, follow_redirects=True))
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT assignee FROM tick WHERE title='T2'")
                             ).scalar() == amy_id


def _field_id(app, table_phys, field_phys):
    from app.metadata.models import MetaTable
    with app.app_context():
        s = SessionLocal()
        t = s.scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        return next(f.id for f in t.fields if f.phys_name == field_phys)


def test_sla_in_lists_and_home(app, client):
    from app.metadata.models import SlaPolicy
    _setup(client)
    tid = _make_table(client, app, "ncase2", "Case", "title")
    _add_field(client, tid, "status", "enum", enum_options="open\nresolved")
    fid = _make_form(client, app, "ncase2_form", "Cases", tid)
    _make_form_p(client, app, "ncase2_view", "Case", tid, "view")
    with app.app_context():
        s = SessionLocal()
        s.add(SlaPolicy(table_id=tid, name="Resolve", active=True, target_minutes=60,
                        status_field_id=_field_id(app, "ncase2", "status"),
                        start_on_create=True, stop_states="resolved"))
        s.commit()

    _ok(client.post(f"/u/forms/{fid}/new", data={"title": "C1", "status": "open"},
                    follow_redirects=True))

    # the list gains an SLA column with time-to-breach
    lst = client.get(f"/u/forms/{fid}").get_data(as_text=True)
    assert ">SLA<" in lst and "m left" in lst

    # the home page shows the soonest-due clock
    home = client.get("/u/").get_data(as_text=True)
    assert "SLA — due next" in home and "C1" in home

    # resolving stops the clock → gone from the panel
    _ok(client.post(f"/u/forms/{fid}/1/edit",
                    data={"title": "C1", "status": "resolved"}, follow_redirects=True))
    assert "SLA — due next" not in client.get("/u/").get_data(as_text=True)


def test_watch_record(app, client):
    from sqlalchemy import func

    from app.metadata.models import Notification
    _setup(client)
    tid = _make_table(client, app, "node", "Node", "name")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "node_form", "Nodes", tid)
    _make_form_p(client, app, "node_view", "Node", tid, "view")
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "core-sw-1"},
                    follow_redirects=True))
    amy = _new_amy(app, client)

    # amy subscribes to boss's record
    assert ">Watch<" in amy.get(f"/u/view/{tid}/1").get_data(as_text=True)
    _ok(amy.post(f"/u/watch/{tid}/1", follow_redirects=True))
    assert ">Unwatch<" in amy.get(f"/u/view/{tid}/1").get_data(as_text=True)

    # any update through the chokepoint notifies the watcher, never the actor
    _ok(client.post(f"/u/forms/{fid}/1/edit", data={"name": "core-sw-1b"},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        amy_id = s.scalar(select(AppUser).where(AppUser.username == "amy")).id
        boss_id = s.scalar(select(AppUser).where(AppUser.username == "boss")).id
        n = s.scalar(select(Notification).where(Notification.user_id == amy_id,
                                                Notification.event == "watch"))
        assert n is not None and "name" in n.body
        assert s.scalar(select(Notification).where(
            Notification.user_id == boss_id, Notification.event == "watch")) is None

    # a comment notifies the watcher too (she never commented herself)
    _ok(client.post(f"/u/comments/{tid}/1",
                    data={"body": "replacing PSU", "visibility": "internal"},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        n = s.scalar(select(Notification).where(Notification.user_id == amy_id,
                                                Notification.event == "comment"))
        assert n is not None and "replacing PSU" in n.body

    # unwatch stops update notifications
    _ok(amy.post(f"/u/watch/{tid}/1", follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/1/edit", data={"name": "core-sw-1c"},
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        assert s.scalar(select(func.count()).select_from(Notification).where(
            Notification.user_id == amy_id, Notification.event == "watch")) == 1


def test_activity_stream(app, client):
    _setup(client)
    tid = _make_table(client, app, "dev", "Dev", "name")
    _ok(client.post(f"/designer/tables/{tid}/flags", data=dict(track_audit="y"),
                    follow_redirects=True))
    fid = _make_form(client, app, "dev_form", "Devs", tid)
    _make_form_p(client, app, "dev_view", "Dev", tid, "view")
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "fw-1"}, follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/1/edit", data={"name": "fw-1b"},
                    follow_redirects=True))
    _ok(client.post(f"/u/comments/{tid}/1",
                    data={"body": "checking", "visibility": "internal"},
                    follow_redirects=True))

    page = client.get("/u/activity").get_data(as_text=True)
    assert "fw-1b" in page                                   # label resolved
    assert ">create<" in page and ">update<" in page and ">comment<" in page
    assert "changed: name" in page and "[internal] checking" in page
    assert "/u/activity" in client.get("/u/").get_data(as_text=True)  # sidebar link

    # filters narrow the feed (amy exists but has done nothing)
    _new_amy(app, client)
    with app.app_context():
        amy_id = SessionLocal().scalar(
            select(AppUser).where(AppUser.username == "amy")).id
    page = client.get(f"/u/activity?user={amy_id}").get_data(as_text=True)
    assert "No activity in this window" in page
