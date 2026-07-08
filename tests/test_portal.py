"""Record conversations (staff ⇄ customer comments) and the customer portal."""
from sqlalchemy import select

from app.db import SessionLocal
from app.metadata.models import AppUser, Comment, Notification
from tests.helpers import (
    _make_form,
    _make_form_p,
    _make_table,
    _new_amy,
    _ok,
    _setup,
)


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
