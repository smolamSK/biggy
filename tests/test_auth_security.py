"""Access control, secrets at rest, MFA, SSO and lockouts. (Split from test_features.py.)"""
import io
import json

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    AppUser,
    Connection,
    DataSource,
    PullSource,
    RateHit,
    Webhook,
)
from tests.helpers import (
    _add_field,
    _b64u,
    _enroll_mfa,
    _fid,
    _make_form,
    _make_table,
    _mint,
    _oidc_setup,
    _oidc_teardown,
    _ok,
    _setup,
    _sign,
    _sso_flow,
)


def test_custom_role_permissions(app, client):
    _setup(client)
    _ok(client.post("/designer/roles", data={"name": "viewer", "label": "Viewer"},
                    follow_redirects=True))
    tid = _make_table(client, app, "doc", "Doc", "title")
    fid = _make_form(client, app, "doc_form", "Docs", tid)
    _ok(client.post("/auth/users/new",
                    data=dict(username="vic", password="pw123456", role="viewer", is_active="y"),
                    follow_redirects=True))
    vic = app.test_client()
    _ok(vic.post("/auth/login", data=dict(username="vic", password="pw123456"),
                 follow_redirects=True))

    _ok(client.post("/designer/permissions", data={f"access_viewer_{fid}": "read"},
                    follow_redirects=True))
    _ok(vic.get(f"/u/forms/{fid}"))                                   # read ok
    assert vic.post(f"/u/forms/{fid}/new", data={"title": "x"}).status_code == 403
    _ok(client.post("/designer/permissions", data={f"access_viewer_{fid}": "none"},
                    follow_redirects=True))
    assert vic.get(f"/u/forms/{fid}").status_code == 403              # no read

def test_self_service_password(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    _ok(amy.get("/auth/account"))
    _ok(amy.post("/auth/account",
                 data={"current": "pw123456", "new": "newpass1", "confirm": "newpass1"},
                 follow_redirects=True))

    old = app.test_client()
    old.post("/auth/login", data=dict(username="amy", password="pw123456"), follow_redirects=True)
    assert old.get("/u/").status_code == 302                          # old password rejected
    new = app.test_client()
    _ok(new.post("/auth/login", data=dict(username="amy", password="newpass1"),
                 follow_redirects=True))
    _ok(new.get("/u/"))

def test_field_permissions(app, client):
    _setup(client)
    _ok(client.post("/designer/roles", data={"name": "viewer", "label": "Viewer"},
                    follow_redirects=True))
    tid = _make_table(client, app, "emp", "Emp", "name")
    _add_field(client, tid, "salary", "integer")
    _add_field(client, tid, "status", "enum", enum_options="active\nleft")
    fid = _make_form(client, app, "emp_form", "Emps", tid)
    salary_fid, status_fid = _fid(app, "emp", "salary"), _fid(app, "emp", "status")
    _ok(client.post(f"/designer/tables/{tid}/field-permissions",
                    data={f"facc_viewer_{salary_fid}": "none", f"facc_viewer_{status_fid}": "read"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new",
                    data={"name": "Ann", "salary": "100", "status": "active"}, follow_redirects=True))
    with app.app_context():
        pk = get_engine().connect().execute(text("SELECT id FROM emp LIMIT 1")).scalar()

    _ok(client.post("/auth/users/new",
                    data=dict(username="vic", password="pw123456", role="viewer", is_active="y"),
                    follow_redirects=True))
    vic = app.test_client()
    _ok(vic.post("/auth/login", data=dict(username="vic", password="pw123456"),
                 follow_redirects=True))

    assert "Salary" not in vic.get(f"/u/forms/{fid}/new").get_data(as_text=True)   # field hidden
    assert "Salary" not in vic.get(f"/u/forms/{fid}").get_data(as_text=True)       # column hidden

    api = app.test_client()
    H = _mint(app, "vic")
    obj = api.get(f"/api/v1/emp/{pk}", headers=H).get_json()
    assert "salary" not in obj and "status" in obj and "name" in obj
    assert api.patch(f"/api/v1/emp/{pk}", json={"salary": 200}, headers=H).status_code == 400
    assert api.patch(f"/api/v1/emp/{pk}", json={"status": "left"}, headers=H).status_code == 400
    _ok(api.patch(f"/api/v1/emp/{pk}", json={"name": "Annie"}, headers=H))         # writable

    Hb = _mint(app, "boss")
    assert "salary" in api.get(f"/api/v1/emp/{pk}", headers=Hb).get_json()         # designer sees all

def test_access_control_roundtrip(app, client):
    _setup(client)
    _ok(client.post("/designer/roles", data={"name": "viewer", "label": "Viewer"},
                    follow_redirects=True))
    tid = _make_table(client, app, "emp", "Emp", "name")
    _add_field(client, tid, "salary", "integer")
    sid = _fid(app, "emp", "salary")
    _ok(client.post(f"/designer/tables/{tid}/field-permissions",
                    data={f"facc_viewer_{sid}": "none"}, follow_redirects=True))

    exp = client.get("/designer/schema/export.json")
    _ok(exp)
    _ok(client.post("/designer/schema/import",
                    data={"file": (io.BytesIO(exp.get_data()), "s.json"), "replace_existing": "y"},
                    content_type="multipart/form-data"))
    with app.app_context():
        from app.metadata.models import MetaFieldPermission, Role
        s = SessionLocal()
        assert s.scalar(select(Role).where(Role.name == "viewer")) is not None
        assert s.scalar(select(MetaFieldPermission).where(MetaFieldPermission.access == "none")) \
            is not None

def test_sql_console_run_reject_and_export(app, client):
    _setup(client)
    _make_table(client, app, "note", "Note", "body")
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO note (body) VALUES ('hello'),('world')"))

    _ok(client.get("/designer/query"))

    run = client.post("/designer/query",
                      data={"sql": "SELECT body FROM note ORDER BY id", "action": "run"})
    _ok(run)
    assert "hello" in run.get_data(as_text=True) and "world" in run.get_data(as_text=True)

    # a non-SELECT is rejected and does NOT execute
    rej = client.post("/designer/query", data={"sql": "DELETE FROM note", "action": "run"})
    _ok(rej)
    assert "Only SELECT" in rej.get_data(as_text=True)
    with app.app_context():
        with get_engine().connect() as c:
            assert c.execute(text("SELECT COUNT(*) FROM note")).scalar() == 2

    exp = client.post("/designer/query",
                      data={"sql": "SELECT body FROM note ORDER BY id", "action": "export"})
    _ok(exp)
    assert exp.mimetype == "text/csv"
    body = exp.get_data(as_text=True)
    assert body.splitlines()[0] == "body" and "hello" in body

def test_sql_console_is_designer_only(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    amy = app.test_client()
    _ok(amy.post("/auth/login", data=dict(username="amy", password="pw123456"),
                 follow_redirects=True))
    assert amy.get("/designer/query").status_code == 403

def test_secrets_encrypted_at_rest(app, client):
    """The 5 secret columns are ciphertext in the DB but plaintext to the ORM."""
    import app.crypto as crypto
    _setup(client)
    tid = _make_table(client, app, "ci", "CI", "name")
    with app.app_context():
        s = SessionLocal()
        conn = Connection(name="peer", base_url="http://x", token="tok-SECRET")
        ds = DataSource(name="src", password="pw-SECRET")
        wh = Webhook(name="wh", target_table_id=tid, token_hash="h" * 16, prefix="pfx",
                     secret="hmac-SECRET")
        ps = PullSource(name="ps", target_table_id=tid, auth_secret="bearer-SECRET",
                        headers='{"Authorization":"Bearer XYZ"}')
        s.add_all([conn, ds, wh, ps])
        s.commit()
        ids = (conn.id, ds.id, wh.id, ps.id)

    checks = [("app_connection", "token", ids[0], "tok-SECRET"),
              ("app_data_source", "password", ids[1], "pw-SECRET"),
              ("app_webhook", "secret", ids[2], "hmac-SECRET"),
              ("app_pull_source", "auth_secret", ids[3], "bearer-SECRET"),
              ("app_pull_source", "headers", ids[3], '{"Authorization":"Bearer XYZ"}')]
    with app.app_context():
        with get_engine().connect() as c:
            for tbl, col, rid, plain in checks:
                raw = c.execute(text(f"SELECT {col} FROM {tbl} WHERE id=:i"), {"i": rid}).scalar()
                assert raw != plain                       # stored as ciphertext
                assert crypto.decrypt(raw) == plain       # which round-trips back
        s = SessionLocal()                                # ORM read returns plaintext
        assert s.get(Connection, ids[0]).token == "tok-SECRET"
        assert s.get(DataSource, ids[1]).password == "pw-SECRET"
        assert s.get(Webhook, ids[2]).secret == "hmac-SECRET"
        assert s.get(PullSource, ids[3]).auth_secret == "bearer-SECRET"
        assert s.get(PullSource, ids[3]).headers == '{"Authorization":"Bearer XYZ"}'

def test_legacy_plaintext_secret_readable(app, client):
    """A secret written before encryption (raw plaintext) still reads back via fallback."""
    _setup(client)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO app_connection (name, base_url, token, active, created_at) "
                           "VALUES ('legacy', 'http://x', 'PLAINTEXT-TOKEN', 1, NOW())"))
        s = SessionLocal()
        conn = s.scalar(select(Connection).where(Connection.name == "legacy"))
        assert conn.token == "PLAINTEXT-TOKEN"            # decrypt-with-fallback

def test_encrypt_secrets_cli(app, client):
    """`encrypt-secrets` migrates legacy plaintext rows to ciphertext."""
    import app.crypto as crypto
    _setup(client)
    with app.app_context():
        with get_engine().begin() as c:
            c.execute(text("INSERT INTO app_connection (name, base_url, token, active, created_at) "
                           "VALUES ('lc', 'http://x', 'LEGACY-PLAIN', 1, NOW())"))
        with get_engine().connect() as c:
            assert c.execute(text("SELECT token FROM app_connection WHERE name='lc'")).scalar() \
                == "LEGACY-PLAIN"

    app.test_cli_runner().invoke(args=["encrypt-secrets"])

    with app.app_context():
        with get_engine().connect() as c:
            raw = c.execute(text("SELECT token FROM app_connection WHERE name='lc'")).scalar()
        assert raw != "LEGACY-PLAIN" and crypto.decrypt(raw) == "LEGACY-PLAIN"
        s = SessionLocal()
        assert s.scalar(select(Connection).where(Connection.name == "lc")).token == "LEGACY-PLAIN"

def test_totp_roundtrip():
    from app import totp
    s = totp.new_secret()
    assert totp.verify(s, totp.now_code(s)) is True
    assert totp.verify(s, "000000") is False or totp.now_code(s) == "000000"
    # window tolerance: the previous 30s step still verifies
    import time as _t
    assert totp.verify(s, totp.now_code(s, at=_t.time() - 30)) is True
    plain, hashed = totp.make_backup_codes(3)
    ok, rest = totp.consume_backup_code(hashed, plain[0])
    assert ok and totp.backup_count(rest) == 2 and totp.consume_backup_code(rest, plain[0])[0] is False

def test_mfa_enroll_and_two_step_login(app, client):
    from app import totp
    _setup(client)                                   # logged in as 'boss'
    secret, backups = _enroll_mfa(app, client)
    assert len(backups) == 10
    with app.app_context():
        u = SessionLocal().scalar(select(AppUser).where(AppUser.username == "boss"))
        assert u.mfa_enabled and u.totp_secret == secret
        with get_engine().connect() as c:
            raw = c.execute(text("SELECT totp_secret FROM app_user WHERE id=:i"), {"i": u.id}).scalar()
        assert raw != secret                         # secret is encrypted at rest

    client.get("/auth/logout")
    # password alone redirects to the second factor and does NOT authenticate
    r = client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    assert r.status_code == 302 and "/auth/mfa-verify" in r.headers["Location"]
    assert client.get("/auth/account").status_code == 302   # still not logged in

    # a wrong code is rejected; the right code logs in
    client.post("/auth/mfa-verify", data={"code": "000001"})
    assert client.get("/auth/account").status_code == 302
    r = client.post("/auth/mfa-verify", data={"code": totp.now_code(secret)})
    assert r.status_code == 302
    assert client.get("/auth/account").status_code == 200   # authenticated now

    # a backup code also works (and is single-use)
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    r = client.post("/auth/mfa-verify", data={"code": backups[0]})
    assert r.status_code == 302 and client.get("/auth/account").status_code == 200
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    client.post("/auth/mfa-verify", data={"code": backups[0]})   # already spent
    assert client.get("/auth/account").status_code == 302       # not accepted twice

def test_mfa_admin_reset(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="amy", password="pw123456", role="user", is_active="y"),
                    follow_redirects=True))
    with app.app_context():
        s = SessionLocal()
        amy = s.scalar(select(AppUser).where(AppUser.username == "amy"))
        amy.mfa_enabled, amy.totp_secret = True, "JBSWY3DPEHPK3PXP"
        s.commit()
        amy_id = amy.id
    _ok(client.post(f"/auth/users/{amy_id}/reset-mfa", follow_redirects=True))
    with app.app_context():
        amy = SessionLocal().get(AppUser, amy_id)
        assert not amy.mfa_enabled and amy.totp_secret is None

def test_require_mfa_enforcement(app, client):
    _setup(client)                                   # boss logged in, no MFA
    app.config["REQUIRE_MFA"] = True
    try:
        r = client.get("/designer/dashboard")
        assert r.status_code == 302 and "/auth/mfa" in r.headers["Location"]
        _ok(client.get("/auth/mfa"))                 # the enroll page itself is reachable
    finally:
        app.config["REQUIRE_MFA"] = False

def test_oidc_login_links_existing(app, client):
    _setup(client)
    _ok(client.post("/auth/users/new",
                    data=dict(username="alice@acme.com", password="pw123456", role="user",
                              is_active="y"), follow_redirects=True))
    client.get("/auth/logout")
    key, holder = _oidc_setup(app)
    try:
        assert "oidc/login" in client.get("/auth/login").get_data(as_text=True)  # SSO button
        r = _sso_flow(client, key, holder, {"email": "alice@acme.com", "sub": "alice-sub"})
        assert r.status_code == 302
        assert client.get("/auth/account").status_code == 200          # logged in
        with app.app_context():
            u = SessionLocal().scalar(select(AppUser).where(AppUser.username == "alice@acme.com"))
            assert u.oidc_subject == "alice-sub"                       # linked for next time
    finally:
        _oidc_teardown(app)

def test_oidc_refuses_unknown_in_link_mode(app, client):
    _setup(client)
    client.get("/auth/logout")
    key, holder = _oidc_setup(app)
    try:
        r = _sso_flow(client, key, holder, {"email": "nobody@acme.com", "sub": "x"})
        assert r.status_code == 302 and "/auth/login" in r.headers["Location"]
        assert client.get("/auth/account").status_code == 302         # not authenticated
        with app.app_context():
            assert SessionLocal().scalar(
                select(AppUser).where(AppUser.username == "nobody@acme.com")) is None
    finally:
        _oidc_teardown(app)

def test_oidc_jit_provision(app, client):
    _setup(client)
    client.get("/auth/logout")
    key, holder = _oidc_setup(app)
    app.config["OIDC_PROVISION"] = "jit"
    try:
        r = _sso_flow(client, key, holder, {"email": "newbie@acme.com", "sub": "new-sub"})
        assert r.status_code == 302 and client.get("/auth/account").status_code == 200
        with app.app_context():
            u = SessionLocal().scalar(select(AppUser).where(AppUser.username == "newbie@acme.com"))
            assert u and u.role == "user" and u.oidc_subject == "new-sub"
    finally:
        _oidc_teardown(app)

def test_oidc_rejects_bad_token(app, client):
    import time as _t
    _setup(client)
    key, holder = _oidc_setup(app)
    try:
        import app.oidc as oidc
        with app.app_context():
            base = {"iss": "https://idp.test", "aud": "cid", "sub": "s", "email": "e@x.y",
                    "exp": int(_t.time()) + 300, "iat": int(_t.time()), "nonce": "N"}
            assert oidc.verify_id_token(_sign(key, base), "N")["sub"] == "s"   # good token

            def _rejected(token, nonce="N"):
                try:
                    oidc.verify_id_token(token, nonce)
                    return False
                except oidc.OidcError:
                    return True

            assert _rejected(_sign(key, dict(base, aud="other")))             # wrong audience
            assert _rejected(_sign(key, dict(base, exp=int(_t.time()) - 100)))  # expired
            assert _rejected(_sign(key, base), nonce="DIFFERENT")             # nonce mismatch
            assert _rejected(_sign(key, dict(base, iss="https://evil.test"))) # issuer mismatch
            tok = _sign(key, base)                                            # tampered signature
            h, p, s = tok.split(".")
            assert _rejected(f"{h}.{p}.{('a' if s[0] != 'a' else 'b') + s[1:]}")
            none_tok = _b64u(json.dumps({"alg": "none"}).encode()) + "." \
                + _b64u(json.dumps(base).encode()) + "."
            assert _rejected(none_tok)                                        # alg 'none' refused

        # state mismatch is refused at the route level
        client.get("/auth/oidc/login")
        r = client.get("/auth/oidc/callback?code=x&state=WRONGSTATE")
        assert r.status_code == 302 and "/auth/login" in r.headers["Location"]
    finally:
        _oidc_teardown(app)

def test_bulk_user_import(app, client):
    _setup(client)
    rows = "alice,user,pw123456\nbob,designer\n# a comment\nboss,user\ncarol,nosuchrole"
    res = client.post("/auth/users/bulk", data={"rows": rows},
                      follow_redirects=True).get_data(as_text=True)
    assert "2 created" in res and "1 skipped" in res and "1 error" in res   # boss exists; bad role
    with app.app_context():
        s = SessionLocal()
        alice = s.scalar(select(AppUser).where(AppUser.username == "alice"))
        bob = s.scalar(select(AppUser).where(AppUser.username == "bob"))
        assert alice.role == "user" and alice.check_password("pw123456")
        assert bob.role == "designer" and not bob.check_password("")        # SSO-only / unusable pw
        assert s.scalar(select(AppUser).where(AppUser.username == "carol")) is None

def test_indexes_created_and_backfilled(app, client):
    """Declared indexes exist after boot and are re-created on existing DBs."""
    from sqlalchemy import inspect as _inspect

    from app.metadata.schema_service import ensure_meta_schema
    _setup(client)
    with app.app_context():
        eng = get_engine()

        def _names(tbl):
            return {i["name"] for i in _inspect(eng).get_indexes(tbl)}

        assert "ix_audit_table_row" in _names("app_audit_log")
        assert {"ix_approval_req_record", "ix_approval_req_state"} <= _names("app_approval_request")
        assert "ix_sla_clock_state" in _names("app_sla_clock")
        # existing-DB path: drop one, ensure_meta_schema puts it back
        with eng.begin() as c:
            c.execute(text("DROP INDEX ix_audit_table_row ON app_audit_log"))
        assert "ix_audit_table_row" not in _names("app_audit_log")
        ensure_meta_schema(eng)
        assert "ix_audit_table_row" in _names("app_audit_log")

def test_login_throttling(app, client):
    """Failed sign-ins lock the account/IP; successes never count."""
    _setup(client)
    client.get("/auth/logout")
    app.config["LOGIN_RATE_LIMIT"] = 3
    try:
        for _ in range(3):
            client.post("/auth/login", data={"username": "boss", "password": "WRONG"})
        # 4th attempt refused even with the CORRECT password
        r = client.post("/auth/login", data={"username": "boss", "password": "secret1"})
        assert "Too many failed attempts" in r.get_data(as_text=True)
        assert client.get("/auth/account").status_code == 302    # still signed out
        with app.app_context():                                  # clear the lockout
            s = SessionLocal()
            s.query(RateHit).delete()
            s.commit()
        _ok(client.post("/auth/login", data={"username": "boss", "password": "secret1"},
                        follow_redirects=True))
        assert client.get("/auth/account").status_code == 200    # signed in
        # successful logins don't accumulate toward a lockout
        with app.app_context():
            assert SessionLocal().scalar(select(func.count()).select_from(RateHit)
                                         .where(RateHit.key.like("login%"))) == 0
    finally:
        app.config["LOGIN_RATE_LIMIT"] = 10

def test_session_lifetime_cookie(app, client):
    """SESSION_LIFETIME_MINUTES makes the login cookie expiring (permanent session)."""
    _setup(client)
    client.get("/auth/logout")
    r = client.post("/auth/login", data={"username": "boss", "password": "secret1"})
    assert "Expires=" not in (r.headers.get("Set-Cookie") or "")   # default: session cookie
    client.get("/auth/logout")
    app.config["SESSION_LIFETIME_MINUTES"] = 60
    try:
        r = client.post("/auth/login", data={"username": "boss", "password": "secret1"})
        assert "Expires=" in (r.headers.get("Set-Cookie") or "")   # now an expiring cookie
    finally:
        app.config["SESSION_LIFETIME_MINUTES"] = 0
