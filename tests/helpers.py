"""Shared helpers for the integration test suite (split from test_features.py)."""
import json
import re

from sqlalchemy import select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    AppUser,
    Connection,
    DataSource,
    MetaForm,
    MetaTable,
    PullSource,
    Webhook,
    Workflow,
)


def _ok(resp):
    assert resp.status_code < 400, resp.get_data(as_text=True)[:300]

def _setup(client):
    _ok(client.post("/setup", data=dict(username="boss", password="secret1",
                                        confirm="secret1"), follow_redirects=True))

def _make_table(client, app, phys, label, field):
    _ok(client.post("/designer/tables/new", data=dict(phys_name=phys, label=label),
                    follow_redirects=True))
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == phys)).id
    _ok(client.post(f"/designer/tables/{tid}/fields",
                    data=dict(phys_name=field, label=field.title(), data_type="string",
                              length=80, nullable="y"), follow_redirects=True))
    return tid

def _make_form(client, app, name, title, tid):
    _ok(client.post("/designer/forms/new",
                    data=dict(name=name, title=title, table_id=tid), follow_redirects=True))
    with app.app_context():
        f = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == name))
        fid, field_ids = f.id, {fd.phys_name: fd.id for fd in f.table.fields}
    for fphys, fid_ in field_ids.items():
        _ok(client.post(f"/designer/forms/{fid}", data=dict(kind="field", field_id=fid_),
                        follow_redirects=True))
    return fid

def _make_form_p(client, app, name, title, tid, purpose="data"):
    _ok(client.post("/designer/forms/new",
                    data=dict(name=name, title=title, table_id=tid, purpose=purpose),
                    follow_redirects=True))
    with app.app_context():
        f = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == name))
        fid, field_ids = f.id, [fd.id for fd in f.table.fields]
    for fid_ in field_ids:
        _ok(client.post(f"/designer/forms/{fid}", data=dict(kind="field", field_id=fid_),
                        follow_redirects=True))
    return fid

def _topo_graph(client, table_id, pk, **params):
    """GET the topology page and return its embedded graph JSON."""
    import json as _json
    import re as _re
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    res = client.get(f"/u/topology/{table_id}/{pk}" + (f"?{qs}" if qs else ""))
    _ok(res)
    m = _re.search(r'id="topo-graph">(.*?)</script>', res.get_data(as_text=True), _re.S)
    return _json.loads(m.group(1)) if m else None

def _add_field(client, tid, phys, dtype, **extra):
    data = dict(phys_name=phys, label=phys.title(), data_type=dtype, nullable="y", **extra)
    _ok(client.post(f"/designer/tables/{tid}/fields", data=data, follow_redirects=True))

def _new_amy(app, client):
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    return amy

def _doc_with_files(client, app):
    """Build a 'doc' table with an image + file field and a form; return ids."""
    tid = _make_table(client, app, "doc", "Doc", "title")
    _add_field(client, tid, "photo", "image")
    _add_field(client, tid, "attach", "file")
    fid = _make_form(client, app, "doc_form", "Docs", tid)
    with app.app_context():
        fields = SessionLocal().scalar(
            select(MetaTable).where(MetaTable.phys_name == "doc")).fields
        fids = {f.phys_name: f.id for f in fields}
    return tid, fid, fids

def _csv_rows(text_body):
    import csv as _csv
    lines = [ln for ln in text_body.splitlines() if ln.strip()]
    return {row[0]: row for row in _csv.reader(lines)}

def _mint(app, username, name="t"):
    from app.api.tokens import mint
    with app.app_context():
        uid = SessionLocal().scalar(select(AppUser).where(AppUser.username == username)).id
        _tok, raw = mint(SessionLocal(), uid, name)
    return {"Authorization": f"Bearer {raw}"}

def _make_workflow(client, app, field_id, transitions, initial):
    _ok(client.post("/designer/workflows", data={"field_id": field_id}, follow_redirects=True))
    with app.app_context():
        wid = SessionLocal().scalar(select(Workflow).where(Workflow.field_id == field_id)).id
    _ok(client.post(f"/designer/workflows/{wid}",
                    json={"transitions": transitions, "layout": {}, "initial": initial}))
    return wid

def _status_field_id(app, table_phys="ticket"):
    with app.app_context():
        t = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        return next(f.id for f in t.fields if f.phys_name == "status")

def _fid(app, table_phys, col):
    with app.app_context():
        t = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        return next(f.id for f in t.fields if f.phys_name == col)

def _make_trigger(app, table_phys, **kw):
    from app.metadata.models import TriggerRule
    with app.app_context():
        s = SessionLocal()
        t = s.scalar(select(MetaTable).where(MetaTable.phys_name == table_phys))
        tr = TriggerRule(table_id=t.id, name=kw.pop("name", "rule"),
                         active=kw.pop("active", True), event=kw.pop("event", "update"), **kw)
        s.add(tr)
        s.commit()
        return tr.id

def _loopback(app):
    """Route connectors' HTTP through a fresh token-only test client (true loopback).

    The connection's base_url is irrelevant — only the path + Bearer token matter.
    Returns nothing; call ``connectors.set_transport(None)`` to restore.
    """
    from urllib.parse import urlsplit

    from app import connectors
    api = app.test_client()

    def transport(method, url, headers, body):
        u = urlsplit(url)
        path = u.path + (("?" + u.query) if u.query else "")
        resp = api.open(path, method=method, headers=dict(headers), data=body,
                        content_type=headers.get("Content-Type"))
        return resp.status_code, resp.get_data(as_text=True)

    connectors.set_transport(transport)

def _raw_token(app, username="boss"):
    return _mint(app, username)["Authorization"].split(" ", 1)[1]

def _make_connection(app, client, token, name="peer"):
    _ok(client.post("/designer/connections",
                    data={"name": name, "base_url": "http://self", "token": token, "active": "y"},
                    follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(Connection).where(Connection.name == name)).id

def _make_feed_orm(app, source_tid, conn_id, target_table, field_map, **kw):
    from app.metadata.models import Feed
    with app.app_context():
        s = SessionLocal()
        feed = Feed(
            name=kw.get("name", "feed"), source_table_id=source_tid, connection_id=conn_id,
            target_table=target_table,
            field_map=json.dumps([{"source": a, "target": b} for a, b in field_map]),
            event=kw.get("event", "create"), mode=kw.get("mode", "create"),
            match_target_field=kw.get("match"), active=True,
            cond_field_id=kw.get("cond_field_id"), cond_op=kw.get("cond_op"),
            cond_value=kw.get("cond_value"), skip_api_writes=kw.get("skip_api_writes", True),
            allow_manual=kw.get("allow_manual", True),
            schedule_minutes=kw.get("schedule_minutes"))
        s.add(feed)
        s.commit()
        return feed.id

def _scalar(app, sql, **params):
    with app.app_context():
        with get_engine().connect() as c:
            return c.execute(text(sql), params).scalar()

def _make_existing_tables(app):
    """Stand up non-Biggy tables (an existing app) directly via SQL."""
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("CREATE TABLE dept (id INT PRIMARY KEY AUTO_INCREMENT, "
                           "name VARCHAR(80))"))
            c.execute(text("CREATE TABLE emp (id INT PRIMARY KEY AUTO_INCREMENT, "
                           "name VARCHAR(80), salary DECIMAL(10,2), dept_id INT, "
                           "CONSTRAINT fk_emp_dept FOREIGN KEY (dept_id) REFERENCES dept(id))"))
            c.execute(text("CREATE TABLE legacy (a INT, b INT, descr VARCHAR(80), "
                           "PRIMARY KEY (a, b))"))
            c.execute(text("CREATE TABLE note (id INT PRIMARY KEY AUTO_INCREMENT, body VARCHAR(200))"))
            c.execute(text("INSERT INTO dept (id, name) VALUES (1, 'Sales')"))

def _make_source(app, client, src2, name="src2"):
    p = src2["params"]
    _ok(client.post("/designer/sources", data={
        "name": name, "driver": p["driver"], "host": p["host"], "port": str(p["port"]),
        "username": p["username"], "password": p["password"], "database": p["database"],
        "active": "y"}, follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(DataSource).where(DataSource.name == name)).id

def _home_tables(app):
    with app.app_context():
        with get_engine().connect() as c:
            return {r[0] for r in c.execute(text("SHOW TABLES")).all()}

def _make_source_generic(app, client, params, name):
    """Create a DataSource from a params dict, omitting blank parts (e.g. SQLite)."""
    data = {"name": name, "driver": params["driver"], "active": "y"}
    for k in ("host", "port", "username", "password", "database"):
        if params.get(k) is not None:
            data[k] = str(params[k])
    _ok(client.post("/designer/sources", data=data, follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(DataSource).where(DataSource.name == name)).id

def _make_webhook(client, app, tid, pairs, mode="create", match_field=None, secret=None,
                  max_body_bytes=None, rate_limit=None, rate_window=None):
    """Create a webhook for ``tid`` mapping ``pairs`` of (json_path, target_col).

    Returns ``(webhook_id, raw_token)`` — the token is parsed from the one-time
    receive URL shown after creation. Optional limit fields are set via the form.
    """
    resp = client.post("/designer/webhooks", data={"table_id": tid}, follow_redirects=True)
    _ok(resp)
    token = re.search(r"/hooks/(whk_[A-Za-z0-9_-]+)", resp.get_data(as_text=True)).group(1)
    with app.app_context():
        wid = SessionLocal().scalar(select(Webhook).order_by(Webhook.id.desc())).id
    data = {"name": "hook", "active": "y", "mode": mode, "user_id": 0,
            "map_source": [s for s, _ in pairs], "map_target": [t for _, t in pairs]}
    if match_field:
        data["match_field"] = match_field
    if secret:
        data["secret"] = secret
    if max_body_bytes is not None:
        data["max_body_bytes"] = max_body_bytes
    if rate_limit is not None:
        data["rate_limit"] = rate_limit
    if rate_window is not None:
        data["rate_window"] = rate_window
    _ok(client.post(f"/designer/webhooks/{wid}", data=data, follow_redirects=True))
    return wid, token

def _force_due(app, model_name, obj_id):
    """Reset a job's last_run_at so it is due again."""
    from app.metadata import models
    with app.app_context():
        s = SessionLocal()
        obj = s.get(getattr(models, model_name), obj_id)
        obj.last_run_at = None
        s.commit()

def _make_pull_orm(app, target_tid, field_map, **kw):
    cfg = kw.get("config")
    with app.app_context():
        s = SessionLocal()
        ps = PullSource(
            name=kw.get("name", "pull"), target_table_id=target_tid, kind=kw.get("kind", "peer"),
            connection_id=kw.get("connection_id"), remote_table=kw.get("remote_table"),
            url=kw.get("url"), records_path=kw.get("records_path"), headers=kw.get("headers"),
            config=json.dumps(cfg) if isinstance(cfg, dict) else cfg,
            auth_secret=kw.get("auth_secret"), watermark=kw.get("watermark"),
            field_map=json.dumps([{"source": a, "target": b} for a, b in field_map]),
            mode=kw.get("mode", "upsert"), match_field=kw.get("match_field"),
            cursor_field=kw.get("cursor_field"), page_size=kw.get("page_size"),
            schedule_minutes=kw.get("schedule_minutes"), active=True)
        s.add(ps)
        s.commit()
        return ps.id

def _ticket_table(client, app):
    tid = _make_table(client, app, "ticket", "Ticket", "title")
    _add_field(client, tid, "status", "enum", enum_options="new\nopen\ndone")
    fid = _make_form(client, app, "ticket_form", "Tickets", tid)
    for t, s in [("A", "new"), ("B", "open"), ("C", "open")]:
        _ok(client.post(f"/u/forms/{fid}/new", data={"title": t, "status": s},
                        follow_redirects=True))
    return tid, fid

def _make_shared_dashboard(client, app, name="Ops"):
    from app.metadata.models import Dashboard
    _ok(client.post("/designer/dashboards", data={"name": name}, follow_redirects=True))
    with app.app_context():
        return SessionLocal().scalar(select(Dashboard).where(Dashboard.name == name)).id

def _add_widget(client, did, **d):
    base = {"kind": "chart", "table_id": 0, "query": "", "chart_type": "bar",
            "content": "", "width": "1", "limit": "5"}
    base.update(d)
    _ok(client.post(f"/designer/dashboards/{did}/widgets", data=base, follow_redirects=True))

def _col(table_phys, pk, col):
    with get_engine().connect() as c:
        return c.execute(text(f"SELECT {col} FROM {table_phys} WHERE id=:i"), {"i": pk}).scalar()

def _login_client(app, username):
    c = app.test_client()
    _ok(c.post("/auth/login", data=dict(username=username, password="pw123456"),
               follow_redirects=True))
    return c

def _pk_of(app, table_phys, title):
    with app.app_context():
        with get_engine().connect() as c:
            return c.execute(text(f"SELECT id FROM {table_phys} WHERE title=:t"),
                             {"t": title}).scalar()

def _enroll_mfa(app, client):
    """Enable MFA for the logged-in user; return (secret, backup_codes)."""
    from app import totp
    page = client.get("/auth/mfa").get_data(as_text=True)
    assert "<svg" in page                            # the setup page renders a scannable QR
    secret = re.search(r"secret=([A-Z2-7]+)", page).group(1)
    resp = client.post("/auth/mfa", data={"action": "enable", "code": totp.now_code(secret)},
                       follow_redirects=True).get_data(as_text=True)
    backups = re.findall(r"[0-9a-f]{4}-[0-9a-f]{4}", resp)
    return secret, backups

def _b64u(b):
    import base64
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

def _oidc_setup(app):
    from cryptography.hazmat.primitives.asymmetric import rsa

    import app.oidc as oidc
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    holder = {"id_token": None}
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "use": "sig", "alg": "RS256",
                      "n": _b64u(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
                      "e": _b64u(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))}]}
    disco = {"issuer": "https://idp.test", "authorization_endpoint": "https://idp.test/auth",
             "token_endpoint": "https://idp.test/token", "jwks_uri": "https://idp.test/jwks"}

    def transport(method, url, headers, body):
        if url.endswith("openid-configuration"):
            return 200, json.dumps(disco)
        if url.endswith("/jwks"):
            return 200, json.dumps(jwks)
        if url.endswith("/token"):
            return 200, json.dumps({"id_token": holder["id_token"], "access_token": "a"})
        return 404, "{}"

    oidc.set_transport(transport)
    oidc.reset_caches()
    app.config.update(OIDC_ISSUER="https://idp.test", OIDC_CLIENT_ID="cid",
                      OIDC_CLIENT_SECRET="sec", OIDC_ENABLED=True, OIDC_PROVISION="link")
    return key, holder

def _oidc_teardown(app):
    import app.oidc as oidc
    oidc.set_transport(None)
    oidc.reset_caches()
    app.config.update(OIDC_ENABLED=False, OIDC_PROVISION="link")

def _sign(key, claims, alg="RS256", kid="k1"):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    head = _b64u(json.dumps({"alg": alg, "kid": kid}).encode())
    payload = _b64u(json.dumps(claims).encode())
    sig = key.sign((head + "." + payload).encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64u(sig)}"

def _sso_flow(client, key, holder, claims):
    """Drive /auth/oidc/login → callback; the token carries the app-issued nonce."""
    import re as _re
    import time as _t
    loc = client.get("/auth/oidc/login").headers["Location"]
    state = _re.search(r"state=([^&]+)", loc).group(1)
    nonce = _re.search(r"nonce=([^&]+)", loc).group(1)
    full = {"iss": "https://idp.test", "aud": "cid", "sub": "sub-1",
            "exp": int(_t.time()) + 300, "iat": int(_t.time()), "nonce": nonce}
    full.update(claims)
    holder["id_token"] = _sign(key, full)
    return client.get(f"/auth/oidc/callback?code=x&state={state}")
