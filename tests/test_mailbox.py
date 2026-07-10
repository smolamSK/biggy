"""Email-to-ticket: mailbox polling, subject parsing, comments and creation."""
import json

from sqlalchemy import select

from app import mailbox as mailbox_svc
from app.db import SessionLocal
from app.metadata.models import AppUser, Attachment, Comment, Mailbox, MetaForm
from tests.helpers import _add_field, _new_amy, _ok, _setup
from tests.test_portal import _mk_company, _mk_user


def _mail(from_addr, subject, body, attach=None, headers=None):
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = from_addr
    m["To"] = "support@example.com"
    m["Subject"] = subject
    for k, v in (headers or {}).items():
        m[k] = v
    m.set_content(body)
    if attach:
        fname, data = attach
        m.add_attachment(data, maintype="application", subtype="octet-stream",
                         filename=fname)
    return bytes(m)


def _mk_mailbox(app, create_form_id=None, aliases=None):
    with app.app_context():
        s = SessionLocal()
        mb = Mailbox(name="support", host="imap.test",
                     aliases=json.dumps(aliases or {"I": "incident"}),
                     create_form_id=create_form_id, schedule_minutes=5)
        s.add(mb)
        s.commit()
        return mb.id


def _poll(app, mid, messages):
    mailbox_svc.set_fetcher(lambda mb: list(messages))
    try:
        with app.app_context():
            s = SessionLocal()
            return mailbox_svc.process_mailbox(s, s.get(Mailbox, mid))
    finally:
        mailbox_svc.set_fetcher(None)


def test_email_to_ticket(app, client):
    from app import mailer
    _setup(client)
    _ok(client.post("/designer/modules/incidents/enable", follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        fid = s.scalar(select(MetaForm).where(MetaForm.name == "incident_form")).id
        tid = s.get(MetaForm, fid).table_id
    _add_field(client, tid, "evidence", "file")
    amy = _new_amy(app, client)
    _ok(amy.post("/auth/account/contact",
                 data={"email": "amy@example.com", "notify_email": "y"},
                 follow_redirects=True))
    _ok(client.post("/auth/account/contact",           # boss gets the reply email
                    data={"email": "boss@example.com", "notify_email": "y"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"title": "Router down", "status": "new",
                          "priority": "P2 - high", "category": "network"},
                    follow_redirects=True))          # becomes INC-0001
    mid = _mk_mailbox(app)
    mailer.OUTBOX.clear()

    # a known sender's reply (full number, quoted tail stripped) → public comment
    n = _poll(app, mid, [_mail("amy@example.com", "Re: [INC-0001] New comment",
                               "Rebooted the router.\n\nOn Thu, boss wrote:\n> old",
                               attach=("log.txt", b"log data"))])
    assert n == 1
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "Rebooted the router." in view and "> old" not in view
    with app.app_context():
        s = SessionLocal()
        c = s.scalar(select(Comment).order_by(Comment.id.desc()))
        amy_id = s.scalar(select(AppUser).where(AppUser.username == "amy")).id
        assert c.user_id == amy_id and not c.internal
        assert s.scalar(select(Attachment)) is not None      # log.txt saved
    # the resulting notification email subject round-trips the number
    assert any("[INC-0001]" in subj for _to, subj, _b in mailer.OUTBOX)

    # alias short form resolves to the same ticket
    assert _poll(app, mid, [_mail("amy@example.com", "i-1 again",
                                  "Alias works")]) == 1
    assert "Alias works" in client.get(f"/u/view/{tid}/1").get_data(as_text=True)

    # unknown sender → internal guest note, never a public comment
    _poll(app, mid, [_mail("stranger@spam.io", "INC-0001 hacked?", "let me in")])
    view = client.get(f"/u/view/{tid}/1").get_data(as_text=True)
    assert "[via email from stranger@spam.io]" in view and "internal note" in view
    with app.app_context():
        c = SessionLocal().scalar(select(Comment).order_by(Comment.id.desc()))
        assert c.internal and c.user_id is None

    # auto-replies are dropped
    before = _comment_count(app)
    _poll(app, mid, [_mail("amy@example.com", "INC-0001 ooo",
                           "I am out of office",
                           headers={"Auto-Submitted": "auto-replied"})])
    assert _comment_count(app) == before


def test_email_creates_and_walls_tenants(app, client):
    _setup(client)
    _ok(client.post("/designer/modules/incidents/enable", follow_redirects=True))
    with app.app_context():
        fid = SessionLocal().scalar(
            select(MetaForm).where(MetaForm.name == "incident_form")).id
        tid = SessionLocal().get(MetaForm, fid).table_id
    acme_id = _mk_company(client, app, "Acme")
    glob_id = _mk_company(client, app, "Globex")
    ann = _mk_user(client, app, "ann", "portal", acme_id)
    _mk_user(client, app, "gil", "portal", glob_id)
    for who, addr in (("ann", "ann@acme.com"), ("gil", "gil@globex.com")):
        with app.app_context():
            s = SessionLocal()
            u = s.scalar(select(AppUser).where(AppUser.username == who))
            u.email = addr
            s.commit()

    # numberless mail from a known portal sender creates a portal-visible ticket
    mid = _mk_mailbox(app, create_form_id=fid)
    assert _poll(app, mid, [_mail("ann@acme.com", "Printer smoking",
                                  "There is smoke.")]) == 1
    home = ann.get("/portal/").get_data(as_text=True)
    assert "Printer smoking" in home
    with app.app_context():
        from sqlalchemy import text

        from app.db import get_engine
        with get_engine().connect() as conn:
            row = conn.execute(text(
                "SELECT number, status, description FROM incident")).mappings().first()
        assert row["number"] and row["status"] == "new"
        assert "smoke" in (row["description"] or "")
        number = row["number"]

    # numberless mail from an unknown sender is dropped (spam guard)
    before = _incident_count(app)
    _poll(app, mid, [_mail("rando@spam.io", "buy stuff", "spam")])
    assert _incident_count(app) == before

    # another tenant replying to a guessed number → internal guest note,
    # invisible in the owner's portal thread
    _poll(app, mid, [_mail("gil@globex.com", f"Re: {number}", "I see your ticket")])
    with app.app_context():
        c = SessionLocal().scalar(select(Comment).order_by(Comment.id.desc()))
        assert c.internal and c.user_id is None
        assert "gil@globex.com" in c.body
    ticket = ann.get(f"/portal/ticket/{tid}/1").get_data(as_text=True)
    assert "I see your ticket" not in ticket


def _comment_count(app):
    from sqlalchemy import func
    from sqlalchemy import select as sel
    with app.app_context():
        return SessionLocal().scalar(sel(func.count()).select_from(Comment))


def _incident_count(app):
    from sqlalchemy import text

    from app.db import get_engine
    with app.app_context():
        with get_engine().connect() as c:
            return c.execute(text("SELECT COUNT(*) FROM incident")).scalar()
