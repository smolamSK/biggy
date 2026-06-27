"""Feed engine: push local records into a remote Biggy peer when they change.

A :class:`~app.metadata.models.Feed` maps a local table to a remote table on a
:class:`~app.metadata.models.Connection`. It fires three ways — on a record event
(reusing the trigger matcher in :mod:`app.triggers`), on a schedule (a watermark
over the source ``id``), or manually. Every push is logged as a
:class:`~app.metadata.models.Notification` (``channel="feed"``), mirroring the
webhook action. A failure is recorded, never raised, so it cannot break a write.

Loopback note: when the peer is *this same* process (tests), pushing runs a
nested API request whose teardown removes the scoped session. So we never hold a
session reference across :func:`connectors.push` — the live session is always
re-acquired from ``SessionLocal`` right where it is used.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import select

from . import connectors, data_service, jobs, triggers
from .db import SessionLocal
from .metadata.models import Connection, Feed, Notification


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _via_api():
    """True when the current write came in through the REST API (loop guard)."""
    try:
        from flask import g
        return bool(g.get("via_api", False))
    except Exception:  # noqa: BLE001 - no app/request context
        return False


# --------------------------------------------------------------------------- #
# Payload + logging
# --------------------------------------------------------------------------- #
def build_payload(feed, row):
    """Map a source ``row`` to the remote payload via ``feed.field_map``.

    A mapping's ``source`` is a source column name, or a string containing
    ``{token}`` placeholders rendered against the row (reuses ``triggers._render``).
    """
    out = {}
    for m in json.loads(feed.field_map or "[]"):
        target, source = m.get("target"), m.get("source")
        if not target or source in (None, ""):
            continue
        out[target] = triggers._render(source, row) if "{" in str(source) else row.get(source)
    return out


def _log(feed, status, target, payload, row_pk, detail):
    s = SessionLocal()
    s.add(Notification(
        event="feed", channel="feed", table_phys=feed.target_table, row_pk=row_pk,
        target=(target or "")[:400] or None, subject=feed.name[:255],
        body=json.dumps(payload, default=str) if payload else None,
        status=status, detail=(detail or "")[:300] or None))
    s.commit()


# --------------------------------------------------------------------------- #
# Run one
# --------------------------------------------------------------------------- #
def run_one(session, engine, feed, row, user_id=None):
    """Push a single source ``row`` through ``feed``. Returns the status string."""
    conn = session.get(Connection, feed.connection_id)
    if not conn or not conn.active:
        _log(feed, "skipped", None, None, row.get("id"), "connection missing or inactive")
        return "skipped"
    payload = build_payload(feed, row)
    match = feed.match_target_field if feed.mode == "upsert" else None
    status, remote_id, detail = connectors.push(conn, feed.target_table, payload, match_field=match)
    _log(feed, status, f"{conn.base_url} → {feed.target_table}", payload, row.get("id"),
         f"{detail} remote_id={remote_id}")
    return status


# --------------------------------------------------------------------------- #
# Event push (hooked into record_service)
# --------------------------------------------------------------------------- #
def feeds_for(session, table_id, events):
    return session.scalars(
        select(Feed).where(Feed.source_table_id == table_id, Feed.active.is_(True),
                           Feed.event.in_(list(events))).order_by(Feed.id)).all()


def run_for_event(session, engine, meta_table, event, pk, old_row, user_id):
    events = {event} | ({"transition"} if event == "update" else set())
    feeds = feeds_for(session, meta_table.id, events)
    if not feeds:
        return
    new_row = (old_row or {}) if event == "delete" \
        else (data_service.get_row(engine, meta_table.phys_name, pk) or {})
    fields = {f.id: f for f in meta_table.fields}
    via_api = _via_api()
    for feed in feeds:
        try:
            if via_api and feed.skip_api_writes:
                continue
            if triggers._matches(feed, event, fields, old_row or {}, new_row) and \
                    triggers._condition_ok(feed, fields, new_row):
                run_one(session, engine, feed, new_row, user_id)
        except Exception as exc:  # noqa: BLE001 - a feed must never break the write
            _log(feed, "failed", None, None, pk, str(exc))


# --------------------------------------------------------------------------- #
# Manual + scheduled (Phases 3 & 4)
# --------------------------------------------------------------------------- #
def run_manual(session, engine, feed, meta_table, pks, user_id=None):
    """Push specific source rows on demand. Returns a list of status strings."""
    out = []
    for pk in pks:
        row = data_service.get_row(engine, meta_table.phys_name, pk)
        if row:
            out.append(run_one(session, engine, feed, row, user_id))
    return out


def _due(feed, now):
    if not feed.schedule_minutes:
        return False
    if not feed.last_run_at:
        return True
    return (now - feed.last_run_at).total_seconds() >= feed.schedule_minutes * 60


def run_scheduled(session, engine, only_feed_id=None):
    """Push source rows newer than each due feed's watermark. Returns rows pushed.

    The source table may live in any data source — its rows are read through that
    source's engine (``engine`` is ignored for the read).
    """
    from .db import engine_for_table
    from .metadata.models import MetaTable
    now = _now()
    q = select(Feed).where(Feed.active.is_(True)).order_by(Feed.id)
    if only_feed_id:
        q = select(Feed).where(Feed.id == only_feed_id)
    pushed = 0
    for feed in session.scalars(q).all():
        # atomic claim so concurrent workers don't push the same rows twice
        if not only_feed_id and not jobs.claim_due(
                session, Feed, feed.id, feed.schedule_minutes, now):
            continue
        mt = session.get(MetaTable, feed.source_table_id)
        if not mt:
            continue
        rows = data_service.list_rows_after(engine_for_table(mt), mt.phys_name, feed.watermark)
        for row in rows:
            run_one(session, engine, feed, row, None)
            pushed += 1
        s = SessionLocal()
        f = s.get(Feed, feed.id)
        if rows:
            f.watermark = max(r["id"] for r in rows)
        f.last_run_at = now
        s.commit()
    return pushed
