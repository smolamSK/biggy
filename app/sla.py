"""SLA engine: per-record service-level clocks with breach detection + escalation.

A :class:`~app.metadata.models.SlaPolicy` defines a target on a table; this module
runs a per-record :class:`~app.metadata.models.SlaClock` that starts/pauses/stops
from the record's status field and measures **24×7** wall-clock time (paused spans
excluded) against the target. The live state (``on_track`` / ``due_soon`` /
``paused`` / ``met`` / ``breached``) and the deadline are written back to designated
record fields via the low-level :mod:`app.data_service` — so they show up in lists,
filters, reports and can drive triggers, and crucially the write-back never re-enters
the :func:`record_service._fire` chokepoint (no recursion), exactly as the trigger
engine's set-field action does.

``run_for_event`` is called from ``record_service._fire`` (wrapped, so it can never
break a write). ``run_breach_sweep`` is called from :func:`app.scheduler.run_due`
(cron / ticker) and reuses the trigger engine's email/notify/set-field primitives to
escalate. Breach granularity equals the scheduler cadence.
"""
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import select

from . import data_service, triggers
from .db import engine_for_table
from .metadata.models import MetaTable, Notification, SlaClock, SlaPolicy

# write-back state tokens
ON_TRACK, DUE_SOON, PAUSED, MET, BREACHED = "on_track", "due_soon", "paused", "met", "breached"
_TERMINAL = ("met", "breached", "stopped")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _int_or_none(pk):
    try:
        return int(pk)
    except (TypeError, ValueError):
        return None


def _states(csv):
    return {s.strip() for s in (csv or "").split(",") if s.strip()}


def _policies(session, table_id):
    return session.scalars(select(SlaPolicy).where(
        SlaPolicy.table_id == table_id, SlaPolicy.active.is_(True))
        .order_by(SlaPolicy.id)).all()


def has_policies(session, table_id):
    return session.scalar(select(SlaPolicy.id).where(
        SlaPolicy.table_id == table_id, SlaPolicy.active.is_(True)).limit(1)) is not None


# --------------------------------------------------------------------------- #
# Helpers shared with the trigger engine
# --------------------------------------------------------------------------- #
def _applies(policy, fields, row):
    if not policy.cond_field_id or not policy.cond_op:
        return True
    f = fields.get(policy.cond_field_id)
    if not f:
        return True
    return triggers._eval(policy.cond_op, row.get(f.phys_name), policy.cond_value)


def _status_value(policy, fields, row):
    if not policy.status_field_id:
        return None
    f = fields.get(policy.status_field_id)
    return row.get(f.phys_name) if f else None


def _classify(policy, status_value):
    """What the clock should be doing for this status: 'stop'|'pause'|'run'|None."""
    v = "" if status_value is None else str(status_value)
    if v in _states(policy.stop_states):
        return "stop"
    if v in _states(policy.pause_states):
        return "pause"
    start = _states(policy.start_states)
    if not start or v in start:
        return "run"
    return None


def _warn_minutes(policy):
    if policy.warn_minutes is not None:
        return policy.warn_minutes
    return current_app.config["SLA_DEFAULT_WARN_MINUTES"]


# --------------------------------------------------------------------------- #
# Clock transitions (24×7: pause converts the remaining deadline to seconds)
# --------------------------------------------------------------------------- #
def _start(clock, policy, now):
    clock.started_at = clock.started_at or now
    clock.due_at = now + timedelta(minutes=policy.target_minutes)
    clock.remaining_seconds = None
    clock.breached_at = None
    clock.state = "running"
    clock.updated_at = now


def _pause(clock, now):
    if clock.due_at:
        clock.remaining_seconds = max(0, int((clock.due_at - now).total_seconds()))
    clock.due_at = None
    clock.state = "paused"
    clock.updated_at = now


def _resume(clock, now):
    rem = clock.remaining_seconds if clock.remaining_seconds is not None else 0
    clock.due_at = now + timedelta(seconds=rem)
    clock.remaining_seconds = None
    clock.state = "running"
    clock.updated_at = now


def _stop(clock, now):
    breached = clock.state == "breached" or (clock.due_at is not None and now > clock.due_at)
    clock.state = "breached" if breached else "met"
    if breached and not clock.breached_at:
        clock.breached_at = clock.due_at or now
    clock.remaining_seconds = None
    clock.updated_at = now


def _state_token(clock, policy, now):
    if clock.state == "breached":
        return BREACHED
    if clock.state == "met":
        return MET
    if clock.state == "stopped":
        return None
    if clock.state == "paused":
        return PAUSED
    if clock.due_at and now >= clock.due_at:
        return BREACHED            # past due but not yet swept
    if clock.due_at and (clock.due_at - now).total_seconds() <= _warn_minutes(policy) * 60:
        return DUE_SOON
    return ON_TRACK


# --------------------------------------------------------------------------- #
# Event handling (called from record_service._fire)
# --------------------------------------------------------------------------- #
def _get_clock(session, policy, table_phys, pk):
    return session.scalar(select(SlaClock).where(
        SlaClock.policy_id == policy.id, SlaClock.table_phys == table_phys,
        SlaClock.row_pk == str(pk)))


def _writeback(session, engine, mt, policy, pk, clock, fields, now):
    updates = {}
    if policy.state_field_id:
        f = fields.get(policy.state_field_id)
        token = _state_token(clock, policy, now)
        if f and token is not None:
            updates[f.phys_name] = token
    if policy.due_field_id:
        f = fields.get(policy.due_field_id)
        if f:
            updates[f.phys_name] = clock.due_at
    if updates:
        data_service.update_row(engine, mt.phys_name, pk, updates)


def _apply(session, engine, mt, policy, event, pk, row, fields):
    now = _now()
    clock = _get_clock(session, policy, mt.phys_name, pk)

    if event == "delete":
        if clock and clock.state in ("running", "paused"):
            clock.state = "stopped"
            clock.updated_at = now
        return

    applies = _applies(policy, fields, row)
    target = _classify(policy, _status_value(policy, fields, row)) if applies else None

    if clock is None:
        starting = applies and target in ("run", "pause") and (
            event != "create" or policy.start_on_create)
        if not starting:
            return
        clock = SlaClock(policy_id=policy.id, table_phys=mt.phys_name, row_pk=str(pk),
                         started_at=now)
        session.add(clock)
        _start(clock, policy, now)
        if target == "pause":
            _pause(clock, now)
    elif clock.state in _TERMINAL:
        pass  # terminal — leave as-is (v1 does not reopen a met/breached clock)
    elif target == "stop":
        _stop(clock, now)
    elif target == "pause":
        if clock.state == "running":
            _pause(clock, now)
    elif target == "run":
        if clock.state == "paused":
            _resume(clock, now)
    elif target is None and clock.state in ("running", "paused"):
        clock.state = "stopped"
        clock.updated_at = now

    _writeback(session, engine, mt, policy, pk, clock, fields, now)


def run_for_event(session, engine, meta_table, event, pk, old_row, user_id):
    """Advance every SLA clock for this record after a create/update/delete."""
    policies = _policies(session, meta_table.id)
    if not policies:
        return
    row = (old_row or {}) if event == "delete" \
        else (data_service.get_row(engine, meta_table.phys_name, pk) or {})
    fields = {f.id: f for f in meta_table.fields}
    for policy in policies:
        try:
            _apply(session, engine, meta_table, policy, event, pk, row, fields)
        except Exception as exc:  # noqa: BLE001 - SLA must never break the write
            session.add(Notification(table_phys=meta_table.phys_name, row_pk=_int_or_none(pk),
                                     event="sla", channel="error", status="failed",
                                     detail=str(exc)[:300]))
    session.commit()


# --------------------------------------------------------------------------- #
# Breach sweep (called from scheduler.run_due) + escalation
# --------------------------------------------------------------------------- #
def _recipient(policy, row):
    if policy.breach_notify_target == "owner":
        return row.get("created_by")
    if policy.breach_notify_target == "user":
        return policy.breach_notify_user_id
    return None  # 'actor' has no meaning in a scheduled sweep


def _escalate(session, engine, mt, policy, clock, row, fields):
    pk = clock.row_pk
    if policy.breach_set_field_id:
        f = fields.get(policy.breach_set_field_id)
        if f:
            val = triggers._set_value(f, policy.breach_set_value)
            data_service.update_row(engine, mt.phys_name, pk, {f.phys_name: val})
            row = data_service.get_row(engine, mt.phys_name, pk) or row
    if policy.breach_in_app:
        uid = _recipient(policy, row)
        if uid:
            session.add(Notification(
                table_phys=mt.phys_name, row_pk=_int_or_none(pk), event="sla_breach",
                channel="in_app", user_id=uid, status="unread",
                body=triggers._render(policy.breach_message or "SLA breached on record {id}", row)))
    if policy.breach_email_to:
        to = triggers._render(policy.breach_email_to, row)
        subject = triggers._render(policy.breach_email_subject or policy.breach_message
                                   or "SLA breached", row)
        body = triggers._render(policy.breach_email_body or policy.breach_message or "", row)
        status, detail = triggers._deliver_email(to, subject, body)
        session.add(Notification(
            table_phys=mt.phys_name, row_pk=_int_or_none(pk), event="sla_breach",
            channel="email", target=(to or "")[:400], subject=(subject or "")[:255],
            body=body, status=status, detail=detail))


def run_breach_sweep(session, now=None):
    """Mark overdue running clocks breached + escalate (once). Returns breach count."""
    now = now or _now()
    breaches = 0
    for clock in session.scalars(select(SlaClock).where(SlaClock.state == "running")).all():
        policy = session.get(SlaPolicy, clock.policy_id)
        mt = session.scalar(select(MetaTable).where(MetaTable.phys_name == clock.table_phys))
        if not policy or not mt:
            continue
        fields = {f.id: f for f in mt.fields}
        try:
            engine = engine_for_table(mt)
            row = data_service.get_row(engine, mt.phys_name, clock.row_pk) or {}
            if clock.due_at and now >= clock.due_at:
                clock.state = "breached"
                clock.breached_at = clock.due_at
                clock.updated_at = now
                if not clock.breach_notified:
                    _escalate(session, engine, mt, policy, clock, row, fields)
                    clock.breach_notified = True
                breaches += 1
            _writeback(session, engine, mt, policy, clock.row_pk, clock, fields, now)
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one bad clock must not stop the sweep
            session.rollback()
            session.add(Notification(table_phys=clock.table_phys, row_pk=_int_or_none(clock.row_pk),
                                     event="sla", channel="error", status="failed",
                                     detail=str(exc)[:300]))
            session.commit()
    return breaches


# --------------------------------------------------------------------------- #
# View panel
# --------------------------------------------------------------------------- #
def clocks_for_record(session, table_id, table_phys, pk):
    """Live SLA status per policy for the record view (empty when no clocks)."""
    now = _now()
    out = []
    for policy in _policies(session, table_id):
        clock = _get_clock(session, policy, table_phys, pk)
        if not clock:
            continue
        remaining = int((clock.due_at - now).total_seconds()) if clock.due_at else None
        out.append({"name": policy.name, "state": clock.state,
                    "token": _state_token(clock, policy, now) or clock.state,
                    "due_at": clock.due_at, "remaining_seconds": remaining,
                    "target_minutes": policy.target_minutes})
    return out
