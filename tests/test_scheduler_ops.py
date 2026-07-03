"""Scheduler jobs, atomic claims, rate-limit store, Docker. (Split from test_features.py.)"""
import io
import json
from datetime import datetime, timezone

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    AppUser,
    Notification,
    RateHit,
    ReportDef,
)
from tests.helpers import (
    _add_field,
    _fid,
    _force_due,
    _loopback,
    _make_connection,
    _make_feed_orm,
    _make_form,
    _make_table,
    _make_trigger,
    _make_webhook,
    _ok,
    _raw_token,
    _setup,
)


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
    from app.metadata.models import Notification
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

def test_scheduler_atomic_claim(app, client):
    """A scheduled job is claimed atomically — concurrent workers run it once."""
    from app import jobs, scheduler
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
