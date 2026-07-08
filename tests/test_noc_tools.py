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
