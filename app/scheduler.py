"""General scheduler: run due time-driven jobs.

Three kinds of work fire because *time passed* rather than on a record event:

  * **scheduled triggers** — a :class:`~app.metadata.models.TriggerRule` with
    ``event="scheduled"`` runs its actions over every row of its table that
    matches the rule's condition (reminders / escalations);
  * **scheduled feeds** — delegated to :func:`app.feeds.run_scheduled` (its own
    watermark over ``id`` + loopback-session handling);
  * **scheduled reports** — a :class:`~app.metadata.models.ReportDef` recomputes
    and emails its result as a digest.

Driven by ``flask run-jobs`` (cron) and, optionally, an in-process ticker thread
(``SCHEDULER_ENABLED``). Every job is isolated — one failure is logged as a
``Notification`` (``channel="error"``), never raised — so a bad rule can't stall
the rest. Idempotency for scheduled triggers is by design: the designer pairs a
**condition** with a **set_field** action so an already-handled row stops matching
(no per-row run ledger needed).
"""
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from sqlalchemy import select
from werkzeug.datastructures import MultiDict

from . import feeds, pull, record_service, reporting, triggers
from .db import SessionLocal, engine_for_table, get_engine
from .metadata.models import AppUser, MetaTable, Notification, ReportDef, TriggerRule


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _due(last_run_at, schedule_minutes, now):
    """True when a job with this cadence is due (never-run counts as due)."""
    if not schedule_minutes or schedule_minutes <= 0:
        return False
    if not last_run_at:
        return True
    return (now - last_run_at).total_seconds() >= schedule_minutes * 60


def _log_error(kind, name, exc):
    s = SessionLocal()  # re-acquire: a failed feed push may have removed the scoped session
    s.add(Notification(channel="error", event="schedule", subject=f"{kind}: {name}"[:255],
                       status="failed", detail=str(exc)[:300]))
    s.commit()


# --------------------------------------------------------------------------- #
# Scheduled triggers
# --------------------------------------------------------------------------- #
def _run_trigger(session, rule):
    """Run a scheduled rule's actions over every matching row. Returns the count."""
    mt = session.get(MetaTable, rule.table_id)
    if not mt:
        return 0
    engine = engine_for_table(mt)
    fields = {f.id: f for f in mt.fields}
    rows, _total = record_service.list_records(
        engine, mt, user_id=None, is_designer=True, per_page=None)
    n = 0
    for row in rows:
        if not triggers._condition_ok(rule, fields, row):
            continue
        pk = row.get(mt.pk_col)
        try:
            triggers._run(session, engine, mt, rule, "scheduled", pk, row, row, None, fields)
            n += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            session.add(Notification(rule_id=rule.id, table_phys=mt.phys_name, row_pk=pk,
                                     event="scheduled", channel="error", status="failed",
                                     detail=str(exc)[:300]))
    return n


# --------------------------------------------------------------------------- #
# Scheduled report digests
# --------------------------------------------------------------------------- #
def _report_recipients(session, report):
    to = [e.strip() for e in (report.recipients or "").split(",") if e.strip()]
    if not to:                                   # fall back to the owner if it looks like an email
        owner = session.get(AppUser, report.user_id)
        if owner and "@" in (owner.username or ""):
            to = [owner.username]
    return to


def _run_report(session, report):
    """Recompute a report and email it as a digest. Returns the number sent."""
    table = session.get(MetaTable, report.table_id)
    if not table:
        return 0
    engine = engine_for_table(table)
    args = MultiDict(parse_qsl(report.query or ""))
    ctx = reporting.build(session, engine, table, args, user=None)  # user=None ⇒ full access
    csv_text = reporting.to_csv(ctx["result"])
    recipients = _report_recipients(session, report)
    if not recipients:
        session.add(Notification(channel="report", event="report", subject=report.name[:255],
                                 status="skipped", detail="no recipients"))
        return 0
    subject = f"[Biggy] {report.name}"
    body = f"Scheduled report '{report.name}':\n\n{csv_text}"
    sent = 0
    for to in recipients:
        status, detail = triggers._deliver_email(to, subject, body)
        session.add(Notification(channel="report", event="report", target=to[:400],
                                 subject=report.name[:255], body=csv_text[:4000],
                                 status=status, detail=detail))
        sent += 1
    return sent


# --------------------------------------------------------------------------- #
# Run all due work
# --------------------------------------------------------------------------- #
def run_due(session, engine, now=None):
    """Run every due scheduled trigger, feed and report. Returns a counts summary."""
    now = now or _now()
    summary = {"triggers": 0, "feeds": 0, "pulls": 0, "reports": 0}

    # 1. scheduled triggers
    for rule in session.scalars(select(TriggerRule).where(
            TriggerRule.active.is_(True), TriggerRule.event == "scheduled")).all():
        if not _due(rule.last_run_at, rule.schedule_minutes, now):
            continue
        try:
            summary["triggers"] += _run_trigger(session, rule)
            rule.last_run_at = now
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            _log_error("trigger", rule.name, exc)
            rule.last_run_at = now
            session.commit()

    # 2. scheduled feeds (delegates; keeps its own watermark + loopback handling)
    try:
        summary["feeds"] = feeds.run_scheduled(session, engine)
    except Exception as exc:  # noqa: BLE001
        _log_error("feed", "scheduled feeds", exc)

    # 2b. scheduled pull sources (poll a remote source → upsert locally)
    try:
        summary["pulls"] = pull.run_scheduled(SessionLocal(), engine)
    except Exception as exc:  # noqa: BLE001
        _log_error("pull", "scheduled pulls", exc)

    # 3. scheduled report digests
    for report in session.scalars(select(ReportDef).where(
            ReportDef.schedule_minutes.is_not(None))).all():
        if not _due(report.last_run_at, report.schedule_minutes, now):
            continue
        try:
            summary["reports"] += _run_report(session, report)
            report.last_run_at = now
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            _log_error("report", report.name, exc)
            report.last_run_at = now
            session.commit()

    return summary


# --------------------------------------------------------------------------- #
# "Run now" (designer UI)
# --------------------------------------------------------------------------- #
def run_one_trigger(session, rule_id):
    rule = session.get(TriggerRule, rule_id)
    if not rule:
        return 0
    n = _run_trigger(session, rule)
    rule.last_run_at = _now()
    session.commit()
    return n


def run_one_report(session, report_id):
    report = session.get(ReportDef, report_id)
    if not report:
        return 0
    n = _run_report(session, report)
    report.last_run_at = _now()
    session.commit()
    return n


# --------------------------------------------------------------------------- #
# In-process ticker (optional; SCHEDULER_ENABLED)
# --------------------------------------------------------------------------- #
_ticker_started = False


def tick_once(app):
    """Run one scheduler pass within an app context (used by the ticker + tests)."""
    with app.app_context():
        return run_due(SessionLocal(), get_engine())


def start_ticker(app):
    """Start a daemon thread that runs due jobs every SCHEDULER_TICK_SECONDS.

    Per-process — fine for this single-process app; under multiple workers each
    would tick, so a single external runner (cron → ``flask run-jobs``) is the
    contract at scale. Off unless ``SCHEDULER_ENABLED`` (and never under TESTING).
    """
    global _ticker_started
    if _ticker_started or not app.config.get("SCHEDULER_ENABLED") or app.config.get("TESTING"):
        return
    _ticker_started = True
    interval = max(5, int(app.config.get("SCHEDULER_TICK_SECONDS", 60)))

    def _loop():
        while True:
            time.sleep(interval)
            try:
                tick_once(app)
            except Exception:  # noqa: BLE001 - the ticker must never die
                pass

    threading.Thread(target=_loop, name="biggy-scheduler", daemon=True).start()
