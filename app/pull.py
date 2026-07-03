"""Pull connectors: poll a remote source and upsert rows into a local table.

The inbound mirror of :mod:`app.feeds` (push out). A :class:`~app.metadata.models.
PullSource` polls a Biggy peer (``kind="peer"``, via a :class:`~app.metadata.models.
Connection`'s ``/api/v1``) or any REST endpoint (``kind="rest"``), maps each remote
record to local fields by **dotted path** and **upserts** it through
:mod:`app.record_service` (so triggers/feeds/formulas fire — chaining). Incremental
via a ``cursor_field`` over a stored ``watermark``; de-duped by ``match_field``.
Run by :mod:`app.scheduler` (``run_due``) and the designer "Run now".

Loopback note (mirrors :mod:`app.feeds`): the remote fetch may run through a Flask
test client whose teardown removes the scoped session, so the session is
re-acquired from ``SessionLocal`` *after* the remote round-trip.
"""
import base64
import json
import re
from datetime import datetime, timezone

from sqlalchemy import select

from . import connectors, data_service, importer, jobs, record_service, triggers
from .db import SessionLocal, engine_for_table
from .metadata.models import Connection, MetaTable, Notification, PullSource

# {token} / {dotted.path} placeholders for templated requests and field sources
_TOKEN = re.compile(r"\{([\w.]+)\}")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dig(record, path):
    """Extract a value from a record by dotted path (``"customer.email"``)."""
    cur = record
    for part in str(path or "").split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _as_raw(value):
    """Render a JSON scalar/structure as the string ``coerce_value`` expects."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _ckey(value, ctype=None):
    """A comparable, never-raising key for a cursor value, honouring ``cursor.type``."""
    if value is None:
        return (0, 0.0, "")
    if ctype == "date":
        try:
            return (1, datetime.fromisoformat(str(value)).timestamp(), "")
        except (TypeError, ValueError):
            return (2, 0.0, str(value))
    if ctype == "string":
        return (2, 0.0, str(value))
    try:                                       # number / auto
        return (1, float(value), "")
    except (TypeError, ValueError):
        return (2, 0.0, str(value))


def _greater(cursor, watermark, ctype=None):
    """True if ``cursor`` is strictly after ``watermark`` (blank watermark ⇒ always)."""
    if watermark in (None, ""):
        return True
    if cursor is None:
        return False
    return _ckey(cursor, ctype) > _ckey(watermark, ctype)


def _advance(watermark, cursor, ctype=None):
    return str(cursor) if cursor is not None and _greater(cursor, watermark, ctype) else watermark


def _sortkey(value, ctype=None):
    return _ckey(value, ctype)


# --------------------------------------------------------------------------- #
# Advanced config (JSON): auth, request templating, pagination, filter, transforms
# --------------------------------------------------------------------------- #
def _cfg(source):
    """Parse the advanced-config JSON (``{}`` when absent/invalid)."""
    if not source.config:
        return {}
    try:
        c = json.loads(source.config)
        return c if isinstance(c, dict) else {}
    except (ValueError, TypeError):
        return {}


def _fill(template, ctx):
    """Substitute ``{watermark}``/``{page}``/… from a flat ctx dict (blank if missing)."""
    if template is None:
        return None
    return _TOKEN.sub(lambda m: "" if ctx.get(m.group(1)) is None else str(ctx.get(m.group(1))),
                      str(template))


def _fill_map(d, ctx):
    return {k: _fill(v, ctx) for k, v in (d or {}).items()}


def _render_record(template, record):
    """Render a ``{dotted.path}`` template against a (possibly nested) remote record."""
    return _TOKEN.sub(
        lambda m: "" if _dig(record, m.group(1)) is None else str(_dig(record, m.group(1))),
        str(template))


def _auth(source, cfg):
    """Build ``(headers, params)`` from the auth preset + the secret column."""
    a = cfg.get("auth") or {}
    atype, secret = a.get("type") or "none", source.auth_secret or ""
    headers, params = {}, {}
    if atype == "bearer":
        headers["Authorization"] = f"Bearer {secret}"
    elif atype == "api_key":
        headers[a.get("header") or "X-API-Key"] = secret
    elif atype == "basic":
        tok = base64.b64encode(f"{a.get('username', '')}:{secret}".encode()).decode()
        headers["Authorization"] = f"Basic {tok}"
    elif atype == "query_key":
        params[a.get("param") or "api_key"] = secret
    return headers, params


def _extract(data, records_path):
    """Pull the records array out of a response body."""
    if records_path:
        arr = _dig(data, records_path)
    elif isinstance(data, list):
        arr = data
    else:
        arr = data.get("data") if isinstance(data, dict) else None
    return [r for r in (arr or []) if isinstance(r, dict)]


# --------------------------------------------------------------------------- #
# Fetch remote records
# --------------------------------------------------------------------------- #
def _peer_records(source, conn, ctype=None):
    """Page a peer table newest-first, stopping at the watermark. Oldest-first list."""
    per_page = source.page_size or 100
    cursor = source.cursor_field
    collected, page = [], 1
    while True:
        params = {"page": page, "per_page": per_page}
        if cursor:
            params["sort"], params["order"] = cursor, "desc"
        status, data = connectors.fetch(conn, source.remote_table, params)
        if status != 200 or not isinstance(data, dict):
            break
        rows = data.get("data") or []
        if not rows:
            break
        stop = False
        for r in rows:
            if cursor and not _greater(_dig(r, cursor), source.watermark, ctype):
                stop = True
                break
            collected.append(r)
        if stop:
            break
        total = data.get("total")
        if total is not None and page * per_page >= total:
            break
        page += 1
    if cursor:
        collected.reverse()            # oldest-first so the watermark advances monotonically
    return collected


def _rest_records(source, cfg, ctype=None):
    """Fetch a REST source (with auth/pagination/templating) → records, oldest-first."""
    req, pg = cfg.get("request") or {}, cfg.get("pagination") or {}
    method = (req.get("method") or "GET").upper()
    style = pg.get("style") or "none"
    max_pages = max(1, int(pg.get("max_pages") or 20))
    per_page = source.page_size or 100
    start = int(pg.get("start") or 1)

    auth_headers, auth_params = _auth(source, cfg)
    base_headers = {}
    if source.headers:
        try:
            base_headers = json.loads(source.headers)
        except ValueError:
            base_headers = {}

    collected, page, cursor_tok, next_url = [], start, None, None
    for _i in range(max_pages):
        ctx = {"watermark": source.watermark or "", "page": page,
               "page_size": per_page, "cursor": cursor_tok or ""}
        params = {**auth_params, **_fill_map(req.get("params"), ctx)}
        if style == "page":
            params[pg.get("param") or "page"] = page
            if pg.get("size_param"):
                params[pg["size_param"]] = per_page
        elif style == "offset":
            params[pg.get("param") or "offset"] = (page - start) * per_page
            if pg.get("size_param"):
                params[pg["size_param"]] = per_page
        elif style == "cursor" and cursor_tok:
            params[pg.get("param") or "cursor"] = cursor_tok
        headers = {**base_headers, **_fill_map(req.get("headers"), ctx), **auth_headers}
        body = _fill(req.get("body"), ctx) if method != "GET" else None
        status, data = connectors.fetch_url(next_url or source.url, headers, params or None,
                                            method=method, body=body)
        if status != 200 or data is None:
            break
        rows = _extract(data, source.records_path)
        collected.extend(rows)
        if style == "none" or not rows:
            break
        if style == "cursor":
            cursor_tok = _dig(data, pg.get("next_path") or "")
            if not cursor_tok:
                break
        elif style == "link":
            next_url = _dig(data, pg.get("next_path") or "")
            if not next_url:
                break
        else:                                   # page / offset
            total = data.get("total") if isinstance(data, dict) else None
            if total is not None and page * per_page >= total:
                break
            page += 1

    if source.cursor_field:
        collected = [r for r in collected
                     if _greater(_dig(r, source.cursor_field), source.watermark, ctype)]
        collected.sort(key=lambda r: _sortkey(_dig(r, source.cursor_field), ctype))
    return collected


def _apply_filter(records, cfg):
    """Keep only records matching ``cfg.filter`` (dotted-path field + trigger op)."""
    f = cfg.get("filter") or {}
    field, op = f.get("field"), f.get("op")
    if not field or not op:
        return records
    return [r for r in records if triggers._eval(op, _dig(r, field), f.get("value"))]


def _remote_records(source, session):
    cfg = _cfg(source)
    ctype = (cfg.get("cursor") or {}).get("type")
    if source.kind == "rest":
        records = _rest_records(source, cfg, ctype)
    else:
        conn = session.get(Connection, source.connection_id) if source.connection_id else None
        records = _peer_records(source, conn, ctype) if conn and conn.active else []
    return _apply_filter(records, cfg)


# --------------------------------------------------------------------------- #
# Map + write
# --------------------------------------------------------------------------- #
def _map_record(rec, field_map, fields, cfg=None):
    """Map a remote record → local values: dotted path or {template} source, then transforms."""
    transforms = (cfg or {}).get("transforms") or {}
    values = {}
    for m in field_map:
        target = m.get("target")
        if not target or target not in fields:
            continue
        src = m.get("source")
        raw = _render_record(src, rec) if src and "{" in str(src) else _as_raw(_dig(rec, src))
        tr = transforms.get(target) or {}
        if tr.get("map") and raw in tr["map"]:          # value mapping (remote → local)
            raw = tr["map"][raw]
        if raw in (None, "") and tr.get("default") is not None:
            raw = tr["default"]
        if raw in (None, ""):
            continue
        values[target] = importer.coerce_value(fields[target], _as_raw(raw))
    return values


def _log(phys, name, status, detail):
    s = SessionLocal()
    s.add(Notification(event="pull", channel="pull_in", table_phys=phys,
                       subject=(name or "")[:255], status=status,
                       detail=(detail or "")[:300] or None))
    s.commit()


def run_one(session, engine, source):
    """Poll ``source`` and upsert its remote records locally. Returns the count."""
    from flask import g
    source_id = source.id
    mt = session.get(MetaTable, source.target_table_id)
    if not mt:
        return 0
    target_engine = engine_for_table(mt)
    phys = mt.phys_name

    records = _remote_records(source, session)   # may detach the scoped session (loopback)

    session = SessionLocal()                     # re-acquire after the remote round-trip
    source = session.get(PullSource, source_id)
    mt = session.get(MetaTable, source.target_table_id)
    fields = {f.phys_name: f for f in mt.fields}
    field_map = json.loads(source.field_map or "[]")
    cfg = _cfg(source)
    ctype = (cfg.get("cursor") or {}).get("type")
    try:
        g.via_api = True                         # loop guard, like the webhook path
    except Exception:  # noqa: BLE001 - no app/request context
        pass

    imported, new_wm = 0, source.watermark
    for rec in records:
        try:
            values = _map_record(rec, field_map, fields, cfg)
            if not values:
                continue
            existing = None
            if source.mode == "upsert" and source.match_field:
                # match_field may be a comma-separated composite key; matching normalized
                existing = data_service.find_id_by_key(target_engine, phys,
                                                       source.match_field, values)
            if existing is not None:
                record_service.update(session, target_engine, mt, existing, values, source.user_id)
            else:
                record_service.create(session, target_engine, mt, values, source.user_id)
            imported += 1
            if source.cursor_field:
                new_wm = _advance(new_wm, _dig(rec, source.cursor_field), ctype)
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the rest
            session.rollback()
            _log(phys, source.name, "failed", str(exc))

    source.watermark = new_wm
    source.last_run_at = _now()
    source.last_status = f"imported {imported}"
    session.commit()
    _log(phys, source.name, "received", f"imported {imported}")
    return imported


# --------------------------------------------------------------------------- #
# Scheduled / manual
# --------------------------------------------------------------------------- #
def _due(source, now):
    if not source.schedule_minutes or source.schedule_minutes <= 0:
        return False
    if not source.last_run_at:
        return True
    return (now - source.last_run_at).total_seconds() >= source.schedule_minutes * 60


def run_scheduled(session, engine, only_source_id=None):
    """Run each active, due pull source (or just ``only_source_id``). Returns rows imported."""
    now = _now()
    q = (select(PullSource).where(PullSource.id == only_source_id) if only_source_id
         else select(PullSource).where(PullSource.active.is_(True)))
    imported = 0
    for source in session.scalars(q).all():
        # atomic claim so concurrent workers don't poll the same source twice
        if not only_source_id and not jobs.claim_due(
                session, PullSource, source.id, source.schedule_minutes, now):
            continue
        imported += run_one(session, engine, source)
    return imported
