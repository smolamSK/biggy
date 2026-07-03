"""Inbound webhook receiver (public, token-authenticated, CSRF-exempt).

``POST /hooks/<token>`` maps a JSON payload onto a record and creates/upserts it
through :mod:`app.record_service` (so triggers, formulas, ripple and audit all
fire). The mirror of :mod:`app.feeds` (outbound). Auth: a secret token in the URL
(only its sha256 is stored, like an API token); an optional per-webhook HMAC
``secret`` additionally requires a valid ``X-Biggy-Signature`` over the raw body.
"""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import select
from werkzeug.exceptions import RequestEntityTooLarge

from .. import data_service, rate_limit, record_service
from ..api.tokens import hash_token
from ..db import SessionLocal, engine_for_table
from ..importer import coerce_value
from ..metadata.models import MetaTable, Notification, Webhook

bp = Blueprint("hooks", __name__, url_prefix="/hooks")

_logger = logging.getLogger(__name__)


def _err(status, message, **headers):
    resp = jsonify(error=message)
    resp.headers.update(headers)
    return resp, status


def _limit(value, cfg_key):
    """Per-webhook override, else the global ``WEBHOOK_*`` config default."""
    return value if value is not None else current_app.config.get(cfg_key)


def _rate_ok(key, limit, window):
    """Shared sliding-window check (DB-backed; see :mod:`app.rate_limit`)."""
    return rate_limit.hit_ok(key, limit, window)


def _dig(payload, path):
    """Extract a value from ``payload`` by dotted path (``"customer.email"``)."""
    cur = payload
    for part in str(path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _as_raw(value):
    """Render a JSON scalar/structure as the string ``coerce_value`` expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _log(session, wh, mt, pk, event, status, detail=None):
    session.add(Notification(
        table_phys=(mt.phys_name if mt else None), row_pk=pk, event=event,
        channel="webhook_in", user_id=wh.user_id, subject=wh.name,
        status=status, detail=detail))
    wh.last_received_at = datetime.now(timezone.utc).replace(tzinfo=None)
    wh.last_status = (detail or status)[:255]
    session.commit()


@bp.route("/<token>", methods=["POST"])
def receive(token):
    session = SessionLocal()
    wh = session.scalar(select(Webhook).where(
        Webhook.token_hash == hash_token(token), Webhook.active.is_(True)))
    if not wh:
        return _err(404, "Unknown webhook.")

    # payload size cap (cheap header check first, then a bounded read)
    max_bytes = _limit(wh.max_body_bytes, "WEBHOOK_MAX_BODY_BYTES")
    if max_bytes and request.content_length and request.content_length > max_bytes:
        return _err(413, "Payload too large.")

    # rate limit (per webhook; before HMAC so a bad-signature flood is throttled too)
    ok, retry = _rate_ok(wh.token_hash,
                         _limit(wh.rate_limit, "WEBHOOK_RATE_LIMIT"),
                         _limit(wh.rate_window, "WEBHOOK_RATE_WINDOW"))
    if not ok:
        return _err(429, "Rate limit exceeded.", **{"Retry-After": str(retry)})

    try:
        if max_bytes:
            request.max_content_length = max_bytes   # bound the read (Werkzeug ≥2.3)
    except (AttributeError, TypeError):              # older Werkzeug: property is read-only
        pass
    try:
        raw = request.get_data()  # bytes — verify the signature over exactly what was sent
    except RequestEntityTooLarge:
        return _err(413, "Payload too large.")
    if max_bytes and len(raw) > max_bytes:
        return _err(413, "Payload too large.")

    if wh.secret:
        expected = "sha256=" + hmac.new(
            wh.secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Biggy-Signature", ""), expected):
            return _err(401, "Invalid or missing signature.")

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _err(400, "Body must be a JSON object.")

    mt = session.get(MetaTable, wh.target_table_id)
    if not mt:
        return _err(404, "Webhook target table is missing.")
    engine = engine_for_table(mt)
    fields = {f.phys_name: f for f in mt.fields}

    values = {}
    for entry in json.loads(wh.field_map or "[]"):
        target = entry.get("target")
        if not target or target not in fields:
            continue
        dug = _dig(payload, entry.get("source"))
        if dug is None:
            continue
        try:
            values[target] = coerce_value(fields[target], _as_raw(dug))
        except ValueError as exc:
            return _err(400, str(exc))
    if not values:
        return _err(400, "Payload mapped no fields.")

    g.via_api = True  # loop guard: feeds skip API/webhook-originated writes

    existing = None
    if wh.mode == "upsert" and wh.match_field:
        try:  # match_field may be a comma-separated composite key; matching is normalized
            existing = data_service.find_id_by_key(engine, mt.phys_name,
                                                   wh.match_field, values)
        except ValueError as exc:
            return _err(400, str(exc))

    try:
        if existing is not None:
            record_service.update(session, engine, mt, existing, values, wh.user_id)
            pk, code, event = existing, 200, "update"
        else:
            pk = record_service.create(session, engine, mt, values, wh.user_id)
            code, event = 201, "create"
    except Exception as exc:  # noqa: BLE001 - surface DB errors (FK, unique, …)
        _logger.warning("webhook '%s' failed to save into %s: %s", wh.name, mt.phys_name, exc)
        session.rollback()    # clear any half-applied metadata writes before logging
        _log(session, wh, mt, None, "create", "failed", str(exc))
        return _err(409, f"Could not save: {exc}")

    _log(session, wh, mt, pk, event, "received")
    return jsonify(ok=True, id=pk, action=event), code
