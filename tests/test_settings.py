"""UI-editable instance settings: registry, secrets, fallbacks, wizard basics."""
from sqlalchemy import select

from app import mailer
from app.db import SessionLocal
from app.metadata.models import AppSetting, AppUser
from tests.helpers import _add_field, _make_form, _make_table, _ok, _setup


def test_settings_registry_secrets_and_fallbacks(app, client):
    from app import settings
    _setup(client)

    _ok(client.post("/designer/settings", data={
        "mail_server": "smtp.example.com", "mail_port": "587", "mail_use_tls": "1",
        "mail_username": "mailer", "mail_password": "s3cr3t-smtp",
        "currency_symbol": "€", "login_rate_limit": "2",
    }, follow_redirects=True))

    with app.app_context():
        s = SessionLocal()
        # secrets are encrypted at rest, decrypted by the getter
        row = s.scalar(select(AppSetting).where(AppSetting.key == "mail_password"))
        assert row is not None and "s3cr3t-smtp" not in (row.value or "")
        with app.test_request_context("/"):
            assert settings.value("mail_password") == "s3cr3t-smtp"
            assert settings.value("mail_server") == "smtp.example.com"
            assert settings.value("mail_port") == 587           # typed
            assert settings.value("mail_use_tls") is True
            # untouched keys fall back to env/config
            assert settings.value("webhook_rate_limit") == \
                app.config["WEBHOOK_RATE_LIMIT"]

    # a blank secret field keeps the stored value; the clear checkbox removes it
    _ok(client.post("/designer/settings", data={"mail_server": "smtp.example.com",
                                                "mail_password": ""},
                    follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(AppSetting).where(
            AppSetting.key == "mail_password")) is not None
    _ok(client.post("/designer/settings", data={"clear_mail_password": "y"},
                    follow_redirects=True))
    with app.app_context():
        assert SessionLocal().scalar(select(AppSetting).where(
            AppSetting.key == "mail_password")) is None

    # the currency symbol is live in rendering
    tid = _make_table(client, app, "quote", "Quote", "name")
    _add_field(client, tid, "price", "currency")
    fid = _make_form(client, app, "quote_form", "Quotes", tid)
    _ok(client.post("/designer/settings", data={"currency_symbol": "€"},
                    follow_redirects=True))
    _ok(client.post(f"/u/forms/{fid}/new", data={"name": "Q1", "price": "12.50"},
                    follow_redirects=True))
    assert "€12.50" in client.get(f"/u/forms/{fid}").get_data(as_text=True)


def test_signin_policy_and_sso_via_settings(app, client):
    _setup(client)
    # lockout after 2 failures, live from the Settings page
    _ok(client.post("/designer/settings", data={"login_rate_limit": "2",
                                                "login_rate_window": "300"},
                    follow_redirects=True))
    anon = app.test_client()
    for _ in range(2):
        anon.post("/auth/login", data={"username": "boss", "password": "wrong"})
    r = anon.post("/auth/login", data={"username": "boss", "password": "wrong"})
    assert "Too many failed attempts" in r.get_data(as_text=True)

    # SSO appears on the sign-in page once issuer + client id are set in the UI
    login = app.test_client().get("/auth/login").get_data(as_text=True)
    assert "Corp login" not in login
    _ok(client.post("/designer/settings", data={
        "oidc_issuer": "https://idp.example.com", "oidc_client_id": "biggy",
        "oidc_button_label": "Corp login",
    }, follow_redirects=True))
    login = app.test_client().get("/auth/login").get_data(as_text=True)
    assert "Corp login" in login and "/auth/oidc/login" in login

    # require-MFA (explicit on) forces enrollment right after sign-in
    _ok(client.post("/designer/settings", data={"require_mfa": "1"},
                    follow_redirects=True))
    r = client.get("/u/", follow_redirects=False)
    assert r.status_code in (301, 302) and "/auth/mfa" in r.headers["Location"]
    _ok(client.post("/designer/settings", data={"require_mfa": "0"},
                    follow_redirects=True))
    _ok(client.get("/u/"))


def test_settings_test_email_button(app, client):
    _setup(client)
    mailer.OUTBOX.clear()
    _ok(client.post("/designer/settings/test-email",
                    data={"to": "ops@example.com"}, follow_redirects=True))
    assert any(to == "ops@example.com" and "test email" in subj.lower()
               for to, subj, _ in mailer.OUTBOX)


def test_setup_wizard_instance_basics(app, client):
    _ok(client.post("/setup", data={
        "username": "boss", "password": "secret1", "confirm": "secret1",
        "admin_email": "boss@example.com", "app_name": "NOC Center",
        "base_url": "https://noc.example.com",
        "mail_server": "smtp.example.com", "mail_use_tls": "y",
        "mail_default_sender": "noc@example.com",
    }, follow_redirects=True))
    with app.app_context():
        from app import settings
        s = SessionLocal()
        boss = s.scalar(select(AppUser).where(AppUser.username == "boss"))
        assert boss.email == "boss@example.com"
        with app.test_request_context("/"):
            assert settings.value("base_url") == "https://noc.example.com"
            assert settings.value("mail_server") == "smtp.example.com"
            assert settings.value("mail_use_tls") is True
            assert settings.value("mail_default_sender") == "noc@example.com"
    assert "NOC Center" in client.get("/u/").get_data(as_text=True)
