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
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import data_service, triggers
from .db import engine_for_table
from .metadata.models import MetaTable, Notification, SlaClock, SlaPolicy

# write-back state tokens
ON_TRACK, DUE_SOON, PAUSED, MET, BREACHED = "on_track", "due_soon", "paused", "met", "breached"
_TERMINAL = ("met", "breached", "stopped")

_log = logging.getLogger(__name__)


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
    from . import settings
    return settings.value("sla_default_warn_minutes")


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
            _log.warning("SLA policy '%s' failed on %s #%s: %s", policy.name,
                         meta_table.phys_name, pk, exc)
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


def _escalation_levels(policy):
    """The policy's escalation chain: a JSON list of level dicts (possibly empty)."""
    try:
        levels = json.loads(policy.escalations or "[]")
    except (ValueError, TypeError):
        return []
    return [lv for lv in levels if isinstance(lv, dict)]


def _fire_escalation(session, mt, policy, clock, row, level):
    """One escalation level: in-app to owner/a user and/or an email."""
    msg = triggers._render(level.get("message")
                           or f"SLA escalation on {mt.label} #{clock.row_pk}", row)
    uid = None
    if level.get("notify_target") == "owner":
        uid = row.get("created_by")
    elif level.get("notify_user_id"):
        uid = level.get("notify_user_id")
    if uid:
        session.add(Notification(table_phys=mt.phys_name, row_pk=_int_or_none(clock.row_pk),
                                 event="escalation", channel="in_app", user_id=uid,
                                 status="unread", body=msg))
    if level.get("email_to"):
        to = triggers._render(level["email_to"], row)
        status, detail = triggers._deliver_email(to, msg, msg)
        session.add(Notification(table_phys=mt.phys_name, row_pk=_int_or_none(clock.row_pk),
                                 event="escalation", channel="email", target=(to or "")[:400],
                                 subject=msg[:255], body=msg, status=status, detail=detail))


def run_breach_sweep(session, now=None):
    """Mark overdue running clocks breached + escalate (once). Returns breach count."""
    now = now or _now()
    breaches = 0
    from . import maintenance
    for clock in session.scalars(select(SlaClock).where(SlaClock.state == "running")).all():
        policy = session.get(SlaPolicy, clock.policy_id)
        mt = session.scalar(select(MetaTable).where(MetaTable.phys_name == clock.table_phys))
        if not policy or not mt:
            continue
        if maintenance.is_active(session, mt, at=now):
            continue                    # planned work: breaches are held
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
            _log.warning("SLA sweep failed for %s #%s: %s", clock.table_phys, clock.row_pk, exc)
            session.rollback()
            session.add(Notification(table_phys=clock.table_phys, row_pk=_int_or_none(clock.row_pk),
                                     event="sla", channel="error", status="failed",
                                     detail=str(exc)[:300]))
            session.commit()

    # escalation chains: for breached clocks, fire the next overdue level (one per pass)
    for clock in session.scalars(select(SlaClock).where(SlaClock.state == "breached")).all():
        policy = session.get(SlaPolicy, clock.policy_id)
        mt = session.scalar(select(MetaTable).where(MetaTable.phys_name == clock.table_phys))
        if not policy or not mt or not clock.breached_at:
            continue
        if maintenance.is_active(session, mt, at=now):
            continue                    # planned work: escalations are held too
        levels = _escalation_levels(policy)
        idx = clock.escalation_level or 0
        if idx >= len(levels):
            continue
        level = levels[idx]
        after = int(level.get("after_minutes") or 0)
        if now < clock.breached_at + timedelta(minutes=after):
            continue
        try:
            engine = engine_for_table(mt)
            row = data_service.get_row(engine, mt.phys_name, clock.row_pk) or {}
            _fire_escalation(session, mt, policy, clock, row, level)
            clock.escalation_level = idx + 1
            clock.updated_at = now
            breaches += 1
            session.commit()
        except Exception as exc:  # noqa: BLE001 - one bad level must not stop the sweep
            _log.warning("SLA escalation failed for %s #%s: %s",
                         clock.table_phys, clock.row_pk, exc)
            session.rollback()
    return breaches


# --------------------------------------------------------------------------- #
# View panel / list column / home panel
# --------------------------------------------------------------------------- #
def _humanize(seconds):
    """'32m left' / 'overdue 2h' — the list/panel SLA cell text."""
    if seconds is None:
        return None
    m = abs(int(seconds)) // 60
    txt = f"{m}m" if m < 120 else f"{m // 60}h"
    return f"{txt} left" if seconds >= 0 else f"overdue {txt}"


_TOKEN_RANK = {BREACHED: 0, DUE_SOON: 1, PAUSED: 2, ON_TRACK: 3, MET: 4}


def clocks_for_rows(session, table_id, table_phys, pks):
    """``{row_pk(str): worst clock summary}`` for a page of list rows.

    One query per page; "worst" = breached first, then soonest due. Powers the
    SLA column on list views.
    """
    policies = {p.id: p for p in _policies(session, table_id)}
    if not policies or not pks:
        return {}
    now = _now()
    far = datetime.max
    out = {}
    for clock in session.scalars(select(SlaClock).where(
            SlaClock.table_phys == table_phys,
            SlaClock.row_pk.in_([str(p) for p in pks]),
            SlaClock.policy_id.in_(policies))):
        policy = policies[clock.policy_id]
        token = _state_token(clock, policy, now)
        if token is None:
            continue
        remaining = int((clock.due_at - now).total_seconds()) if clock.due_at else None
        entry = {"token": token, "due_at": clock.due_at,
                 "text": _humanize(remaining) or token.replace("_", " "),
                 "_key": (_TOKEN_RANK.get(token, 9), clock.due_at or far)}
        cur = out.get(clock.row_pk)
        if cur is None or entry["_key"] < cur["_key"]:
            out[clock.row_pk] = entry
    return out


def breaching_next(session, user, limit=8):
    """The soonest-due active/overdue clocks across tables the user can read."""
    from . import helpers
    now = _now()
    tables = {t.id: t for t in session.scalars(select(MetaTable))}
    policies = {p.id: p for p in session.scalars(
        select(SlaPolicy).where(SlaPolicy.active.is_(True)))}
    readable, out = {}, []
    for clock in session.scalars(select(SlaClock).where(
            SlaClock.state.in_(("running", "paused", "breached")),
            SlaClock.due_at.isnot(None))
            .order_by(SlaClock.due_at).limit(limit * 5)):
        policy = policies.get(clock.policy_id)
        table = tables.get(policy.table_id) if policy else None
        if table is None:
            continue
        if table.id not in readable:
            readable[table.id] = helpers.table_readable(session, user, table)
        if not readable[table.id]:
            continue
        remaining = int((clock.due_at - now).total_seconds())
        out.append({"table": table, "pk": clock.row_pk, "policy": policy.name,
                    "due_at": clock.due_at,
                    "token": _state_token(clock, policy, now),
                    "text": _humanize(remaining)})
        if len(out) >= limit:
            break
    return out


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
