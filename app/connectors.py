"""Outbound HTTP client to a peer Biggy instance's REST API (``/api/v1``).

Used by the feed engine (:mod:`app.feeds`) to push records into another,
independently-running Biggy product. The actual transport is indirected through
:data:`TRANSPORT` so tests can drive it through a Flask **test client** (a true
loopback against the same DB) instead of real sockets. With the default urllib
transport under ``TESTING`` the calls are *skipped* — mirroring the webhook
deliverer in :mod:`app.triggers` — so nothing touches the network by accident.
"""
import json
import urllib.error
import urllib.request
from urllib.parse import urlencode

from flask import current_app


def _urllib_transport(method, url, headers, body):
    """Default transport: real HTTP. Returns ``(status_int, body_text)``."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_timeout()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


TRANSPORT = _urllib_transport


def set_transport(fn):
    """Override the HTTP transport (used by tests). ``None`` restores urllib."""
    global TRANSPORT
    TRANSPORT = fn or _urllib_transport


def _timeout():
    try:
        return current_app.config.get("NOTIFY_WEBHOOK_TIMEOUT", 5)
    except RuntimeError:
        return 5


def _live():
    """Whether outbound calls should really run (skip the default transport under TESTING)."""
    try:
        testing = current_app.config.get("TESTING")
    except RuntimeError:
        testing = False
    return not (testing and TRANSPORT is _urllib_transport)


def _api_base(conn):
    return conn.base_url.rstrip("/") + "/api/v1"


def _request(conn, method, path, params=None, payload=None):
    url = _api_base(conn) + path
    if params:
        url += "?" + urlencode(params)
    headers = {"Authorization": f"Bearer {conn.token or ''}", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload, default=str).encode("utf-8")
        headers["Content-Type"] = "application/json"
    status, text = TRANSPORT(method, url, headers, body)
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = {"raw": text[:300]}
    return status, data


# --------------------------------------------------------------------------- #
# Public operations
# --------------------------------------------------------------------------- #
def ping(conn):
    """Verify the peer + token are reachable. Returns ``(ok, detail, [table names])``."""
    if not _live():
        return False, "skipped (testing)", []
    try:
        status, data = _request(conn, "GET", "/")
    except Exception as exc:  # noqa: BLE001 - surface any transport error to the UI
        return False, str(exc)[:255], []
    if status == 200 and isinstance(data, dict):
        return True, "OK", [t["name"] for t in data.get("tables", [])]
    return False, f"HTTP {status}", []


def remote_fields(conn, table):
    """Field descriptors for a remote table (for the mapping UI). ``[]`` on failure."""
    if not _live():
        return []
    try:
        status, data = _request(conn, "GET", f"/{table}/fields")
    except Exception:  # noqa: BLE001
        return []
    if status == 200 and isinstance(data, dict):
        return data.get("fields", [])
    return []


def fetch(conn, table, params=None):
    """GET a peer table's records (for pull connectors). Returns ``(status, data)``.

    ``data`` is the peer's list envelope ``{data:[...], page, per_page, total}``.
    Returns ``(0, None)`` when calls are skipped (TESTING + default transport).
    """
    if not _live():
        return 0, None
    try:
        return _request(conn, "GET", f"/{table}", params=params or None)
    except Exception as exc:  # noqa: BLE001 - surface transport errors to the caller
        return 0, {"error": str(exc)[:255]}


def fetch_url(url, headers=None, params=None, method="GET", body=None):
    """Call an arbitrary REST URL and JSON-decode it. Returns ``(status, data)``.

    Routed through :data:`TRANSPORT` so tests can inject a loopback. Skipped
    (``(0, None)``) under TESTING with the default urllib transport. ``body`` (a
    str/bytes JSON payload) is sent for non-GET requests (e.g. a POST query).
    """
    if not _live():
        return 0, None
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    hdrs = {"Accept": "application/json", **(headers or {})}
    data_bytes = None
    if body is not None:
        data_bytes = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    try:
        status, text = TRANSPORT(method, url, hdrs, data_bytes)
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)[:255]}
    data = None
    if text:
        try:
            data = json.loads(text)
        except ValueError:
            data = {"raw": text[:300]}
    return status, data


def push(conn, table, payload, match_field=None):
    """Create or upsert a record on the peer. Returns ``(status, remote_id, detail)``.

    ``status`` is ``sent`` | ``failed`` | ``skipped``. With ``match_field`` set
    and present in ``payload``, an existing remote row is looked up and PATCHed;
    otherwise a new row is POSTed.
    """
    if not _live():
        return "skipped", None, "skipped (testing)"
    try:
        existing_id = None
        if match_field and payload.get(match_field) not in (None, ""):
            s, data = _request(conn, "GET", f"/{table}",
                               params={match_field: payload[match_field]})
            if s == 200 and isinstance(data, dict) and data.get("data"):
                existing_id = data["data"][0].get("id")
        if existing_id:
            s, data = _request(conn, "PATCH", f"/{table}/{existing_id}", payload=payload)
            ok = s == 200
        else:
            s, data = _request(conn, "POST", f"/{table}", payload=payload)
            ok = s == 201
    except Exception as exc:  # noqa: BLE001
        return "failed", None, str(exc)[:255]
    if ok:
        rid = (data or {}).get("id", existing_id)
        return "sent", rid, f"HTTP {s}"
    err = (data or {}).get("error") if isinstance(data, dict) else None
    return "failed", existing_id, f"HTTP {s}: {err or ''}".strip()
