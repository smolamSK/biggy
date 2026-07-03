"""Trigger engine: when a record event fires, run a rule's notification actions.

Called from :mod:`app.record_service` (create / update / remove), so the form,
inline edit, Kanban drag and the REST API all go through it. Every action is
recorded as a :class:`~app.metadata.models.Notification` (the source of truth +
the in-app inbox); email/webhook delivery is best-effort and **skipped when not
configured or under TESTING**, so the engine never needs the network to work.
An action failure is logged, never raised — it must not break the write.
"""
import contextvars
import json
import logging
import re
import smtplib
import urllib.request
from datetime import date, datetime, timezone
from email.message import EmailMessage

from flask import current_app
from sqlalchemy import select

from . import data_service
from .metadata.models import Notification, TriggerRule

_log = logging.getLogger(__name__)


_TOKEN = re.compile(r"\{(\w+)\}")

# Depth of trigger-chained record creation (create-record action -> triggers on the
# new record -> ...). Caps runaway chains; contextvar = safe under threaded workers.
_CREATE_DEPTH = contextvars.ContextVar("trigger_create_depth", default=0)
_MAX_CREATE_DEPTH = 3


def has_rules(session, table_id):
    return session.scalar(
        select(TriggerRule.id).where(TriggerRule.table_id == table_id,
                                     TriggerRule.active.is_(True)).limit(1)) is not None


def rules_for(session, table_id, events):
    return session.scalars(
        select(TriggerRule).where(TriggerRule.table_id == table_id,
                                  TriggerRule.active.is_(True),
                                  TriggerRule.event.in_(list(events)))
        .order_by(TriggerRule.id)).all()


def fire(session, engine, meta_table, event, pk, old_row, user_id):
    events = {event} | ({"transition"} if event == "update" else set())
    rules = rules_for(session, meta_table.id, events)
    if not rules:
        return
    new_row = (old_row or {}) if event == "delete" \
        else (data_service.get_row(engine, meta_table.phys_name, pk) or {})
    fields = {f.id: f for f in meta_table.fields}
    for rule in rules:
        try:
            if _matches(rule, event, fields, old_row or {}, new_row) and \
                    _condition_ok(rule, fields, new_row):
                new_row = _run(session, engine, meta_table, rule, event, pk,
                               old_row, new_row, user_id, fields)
        except Exception as exc:  # noqa: BLE001 - a rule must never break the write
            _log.warning("trigger rule '%s' failed on %s #%s: %s", rule.name,
                         meta_table.phys_name, pk, exc)
            session.add(Notification(rule_id=rule.id, table_phys=meta_table.phys_name,
                                     row_pk=pk, event=event, channel="error",
                                     status="failed", detail=str(exc)[:300]))
    session.commit()


# --------------------------------------------------------------------------- #
def _matches(rule, event, fields, old_row, new_row):
    if rule.event == "transition":
        if event != "update":
            return False
        f = fields.get(rule.field_id)
        if not f:
            return False
        ov, nv = old_row.get(f.phys_name), new_row.get(f.phys_name)
        if ov == nv:
            return False
        if rule.from_state and ov != rule.from_state:
            return False
        if rule.to_state and nv != rule.to_state:
            return False
        return True
    return rule.event == event


def _condition_ok(rule, fields, row):
    if not rule.cond_field_id or not rule.cond_op:
        return True
    f = fields.get(rule.cond_field_id)
    if not f:
        return True
    return _eval(rule.cond_op, row.get(f.phys_name), rule.cond_value)


def _eval(op, val, target):
    s, t = ("" if val is None else str(val)), (target or "")
    if op == "eq":
        return s == t
    if op == "ne":
        return s != t
    if op == "contains":
        return t in s
    if op == "not_contains":
        return t not in s
    if op == "starts_with":
        return s.startswith(t)
    if op == "ends_with":
        return s.endswith(t)
    if op == "empty":
        return val in (None, "")
    if op == "not_empty":
        return val not in (None, "")
    if op == "is_true":
        return bool(val) is True or val == 1
    if op == "is_false":
        return val in (False, 0)
    try:
        fv, ft = float(val), float(t)
    except (TypeError, ValueError):
        return False
    return {"gt": fv > ft, "gte": fv >= ft, "lt": fv < ft, "lte": fv <= ft}.get(op, False)


def _render(template, row):
    if not template:
        return ""
    return _TOKEN.sub(
        lambda m: "" if row.get(m.group(1)) is None else str(row.get(m.group(1))), template)


def _set_value(field, raw):
    from . import importer
    low = (raw or "").strip().lower()
    if low == "now":
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if low == "today":
        return date.today()
    try:
        return importer.coerce_value(field, raw)
    except ValueError:
        return raw


def _recipient(rule, row, user_id):
    if rule.notify_target == "owner":
        return row.get("created_by")
    if rule.notify_target == "user":
        return rule.notify_user_id
    return user_id  # 'actor' (default)


def _notif(session, rule, mt, pk, event, channel, **kw):
    session.add(Notification(rule_id=rule.id, table_phys=mt.phys_name, row_pk=pk,
                             event=event, channel=channel, **kw))


def _run(session, engine, mt, rule, event, pk, old_row, new_row, user_id, fields):
    if rule.set_field_id and event != "delete":
        f = fields.get(rule.set_field_id)
        if f:
            value = _set_value(f, rule.set_value)
            try:
                data_service.update_row(engine, mt.phys_name, pk, {f.phys_name: value})
                new_row = data_service.get_row(engine, mt.phys_name, pk) or new_row
                _notif(session, rule, mt, pk, event, "set_field", status="sent",
                       body=f"{f.phys_name} = {value}")
            except Exception as exc:  # noqa: BLE001
                _notif(session, rule, mt, pk, event, "set_field", status="failed",
                       detail=str(exc)[:300])

    if rule.in_app:
        uid = _recipient(rule, new_row, user_id)
        if uid:
            _notif(session, rule, mt, pk, event, "in_app", user_id=uid, status="unread",
                   body=_render(rule.message or "Update on record {id}", new_row))

    if rule.email_to:
        to = _render(rule.email_to, new_row)
        subject = _render(rule.email_subject or rule.message or "", new_row)
        body = _render(rule.email_body or rule.message or "", new_row)
        status, detail = _deliver_email(to, subject, body)
        _notif(session, rule, mt, pk, event, "email", target=to, subject=subject,
               body=body, status=status, detail=detail)

    if rule.webhook_url:
        payload = _webhook_payload(rule, event, mt, new_row, old_row)
        status, detail = _deliver_webhook(rule.webhook_url, payload)
        _notif(session, rule, mt, pk, event, "webhook", target=rule.webhook_url,
               body=json.dumps(payload, default=str), status=status, detail=detail)

    if rule.create_table_id and event != "delete":
        _create_record(session, rule, mt, pk, event, new_row, user_id)
    return new_row


def _webhook_payload(rule, event, mt, new_row, old_row):
    """The POST body: full event JSON, or {"text": message} for Slack/Teams."""
    if rule.webhook_format == "text":
        return {"text": _render(rule.message or f"{mt.label}: {event} on record {{id}}",
                                new_row)}
    return {"event": event, "table": mt.phys_name,
            "record": _jsonable(new_row), "old": _jsonable(old_row) if old_row else None}


def _create_record(session, rule, mt, pk, event, new_row, user_id):
    """The create-record action: build values from the field map and insert.

    Runs through ``record_service.create`` (so audit/triggers/SLA apply to the new
    record); a contextvar depth cap stops trigger-chained creation loops.
    """
    from . import record_service
    from .db import engine_for_table
    from .metadata.models import MetaTable

    depth = _CREATE_DEPTH.get()
    if depth >= _MAX_CREATE_DEPTH:
        _notif(session, rule, mt, pk, event, "error", status="skipped",
               detail=f"create-record chain depth cap ({_MAX_CREATE_DEPTH}) reached")
        return
    target = session.get(MetaTable, rule.create_table_id)
    if not target:
        return
    try:
        fmap = json.loads(rule.create_field_map or "[]")
    except (ValueError, TypeError):
        fmap = []
    target_fields = {f.phys_name: f for f in target.fields}
    values = {}
    for m in fmap:
        f = target_fields.get((m or {}).get("target"))
        if f is None:
            continue
        values[f.phys_name] = _set_value(f, _render(str(m.get("source", "")), new_row))
    if not values:
        return
    token = _CREATE_DEPTH.set(depth + 1)
    try:
        new_pk = record_service.create(session, engine_for_table(target), target,
                                       values, user_id)
        _notif(session, rule, mt, pk, event, "set_field", status="sent",
               body=f"created {target.phys_name} #{new_pk}")
    except Exception as exc:  # noqa: BLE001 - the action must never break the write
        _notif(session, rule, mt, pk, event, "error", status="failed",
               detail=f"create-record: {exc}"[:300])
    finally:
        _CREATE_DEPTH.reset(token)


def _jsonable(row):
    from .api.serialization import serialize_row
    return serialize_row(row) if row else None


def _deliver_email(to, subject, body):
    cfg = current_app.config
    if current_app.config.get("TESTING") or not cfg.get("MAIL_SERVER") or not to:
        return "skipped", None
    try:
        msg = EmailMessage()
        msg["From"] = cfg.get("MAIL_DEFAULT_SENDER", "biggy@localhost")
        msg["To"] = to
        msg["Subject"] = subject or ""
        msg.set_content(body or "")
        timeout = cfg.get("NOTIFY_WEBHOOK_TIMEOUT", 5)
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg.get("MAIL_PORT", 25), timeout=timeout) as s:
            if cfg.get("MAIL_USE_TLS"):
                s.starttls()
            if cfg.get("MAIL_USERNAME"):
                s.login(cfg["MAIL_USERNAME"], cfg.get("MAIL_PASSWORD", ""))
            s.send_message(msg)
        return "sent", None
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)[:300]


def _deliver_webhook(url, payload):
    if current_app.config.get("TESTING") or not url:
        return "skipped", None
    try:
        data = json.dumps(payload, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(
                req, timeout=current_app.config.get("NOTIFY_WEBHOOK_TIMEOUT", 5)) as r:
            return ("sent" if 200 <= r.status < 300 else "failed"), f"HTTP {r.status}"
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)[:300]
