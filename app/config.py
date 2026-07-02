"""Application configuration, loaded from environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Base config read from environment variables."""

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

    # --- Database connection (configurable; default = local MariaDB) ---
    # A full DATABASE_URL takes precedence if provided.
    DATABASE_URL = os.environ.get("DATABASE_URL")
    DB_DRIVER = os.environ.get("DB_DRIVER", "mysql+pymysql")
    DB_HOST = os.environ.get("DB_HOST", "127.0.0.1")
    DB_PORT = int(os.environ.get("DB_PORT", "3306"))
    DB_USER = os.environ.get("DB_USER", "biggy")
    DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
    DB_NAME = os.environ.get("DB_NAME", "biggy")

    # Cap uploaded file size (CSV import, attachments)
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # Where uploaded attachments are stored. Defaults to <instance>/uploads
    # (set in create_app) unless overridden here.
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER")

    # Notification delivery (all optional; email is skipped unless MAIL_SERVER set)
    MAIL_SERVER = os.environ.get("MAIL_SERVER")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "25"))
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")
    MAIL_USE_TLS = _as_bool(os.environ.get("MAIL_USE_TLS"))
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "biggy@localhost")
    NOTIFY_WEBHOOK_TIMEOUT = int(os.environ.get("NOTIFY_WEBHOOK_TIMEOUT", "5"))

    # Inbound-webhook abuse limits (defaults; each webhook may override in the UI)
    WEBHOOK_MAX_BODY_BYTES = int(os.environ.get("WEBHOOK_MAX_BODY_BYTES", str(64 * 1024)))
    WEBHOOK_RATE_LIMIT = int(os.environ.get("WEBHOOK_RATE_LIMIT", "120"))  # per window, 0 = off
    WEBHOOK_RATE_WINDOW = int(os.environ.get("WEBHOOK_RATE_WINDOW", "60"))  # window seconds

    # Scheduler: run due jobs (scheduled triggers / feeds / report digests).
    # Off by default — driven by `flask run-jobs` (cron). Enable to also run an
    # in-process background ticker (single-process deployments only).
    SCHEDULER_ENABLED = _as_bool(os.environ.get("SCHEDULER_ENABLED"))
    SCHEDULER_TICK_SECONDS = int(os.environ.get("SCHEDULER_TICK_SECONDS", "60"))

    # SLA engine: when a policy leaves warn_minutes blank, this is the "due soon"
    # threshold (minutes before the deadline) for the on_track→due_soon state.
    SLA_DEFAULT_WARN_MINUTES = int(os.environ.get("SLA_DEFAULT_WARN_MINUTES", "30"))

    # Require TOTP two-factor for every user (operator policy). When true, an
    # authenticated user without MFA is forced to enroll before using the app.
    REQUIRE_MFA = _as_bool(os.environ.get("REQUIRE_MFA"))

    # Login lockout: after N *failed* attempts (per username and per IP) within the
    # window, further sign-in attempts are refused. 0 disables. Successful logins
    # never count. Also bounds wrong MFA-code attempts.
    LOGIN_RATE_LIMIT = int(os.environ.get("LOGIN_RATE_LIMIT", "10"))
    LOGIN_RATE_WINDOW = int(os.environ.get("LOGIN_RATE_WINDOW", "300"))  # seconds

    # SSO via OpenID Connect (see app/oidc.py). Enabled once issuer + client id are set.
    OIDC_ISSUER = os.environ.get("OIDC_ISSUER")
    OIDC_CLIENT_ID = os.environ.get("OIDC_CLIENT_ID")
    OIDC_CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET")
    OIDC_SCOPES = os.environ.get("OIDC_SCOPES", "openid email profile")
    OIDC_REDIRECT_URI = os.environ.get("OIDC_REDIRECT_URI")  # blank → derived from the request
    OIDC_USERNAME_CLAIM = os.environ.get("OIDC_USERNAME_CLAIM", "email")
    OIDC_PROVISION = os.environ.get("OIDC_PROVISION", "link")  # link | jit
    OIDC_DEFAULT_ROLE = os.environ.get("OIDC_DEFAULT_ROLE", "user")
    OIDC_BUTTON_LABEL = os.environ.get("OIDC_BUTTON_LABEL", "Sign in with SSO")
    OIDC_ENABLED = bool(OIDC_ISSUER and OIDC_CLIENT_ID)

    # Encryption at rest for secret columns. A urlsafe-base64 Fernet key; when blank,
    # a stable key is derived from SECRET_KEY (rotating either makes old ciphertext
    # unreadable — see app/crypto.py and `flask encrypt-secrets`).
    BIGGY_ENCRYPTION_KEY = os.environ.get("BIGGY_ENCRYPTION_KEY")

    # Dependency / impact map (topology view). Depth is user-adjustable in the UI,
    # clamped to MAX_DEPTH; MAX_NODES bounds query fan-out for a single map.
    TOPOLOGY_DEFAULT_DEPTH = int(os.environ.get("TOPOLOGY_DEFAULT_DEPTH", "2"))
    TOPOLOGY_MAX_DEPTH = int(os.environ.get("TOPOLOGY_MAX_DEPTH", "4"))
    TOPOLOGY_MAX_NODES = int(os.environ.get("TOPOLOGY_MAX_NODES", "150"))

    # Display symbol for the 'currency' field type
    CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "$")

    # Session / cookie hardening
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Set true when serving over HTTPS (cookies then never travel over http).
    SESSION_COOKIE_SECURE = _as_bool(os.environ.get("SESSION_COOKIE_SECURE"))
    # Blank/0 = a browser-session cookie (expires on close, the default). When set,
    # sessions become permanent with this lifetime (cookie + server side).
    SESSION_LIFETIME_MINUTES = int(os.environ.get("SESSION_LIFETIME_MINUTES", "0"))
    if SESSION_LIFETIME_MINUTES > 0:
        from datetime import timedelta as _td
        PERMANENT_SESSION_LIFETIME = _td(minutes=SESSION_LIFETIME_MINUTES)
    WTF_CSRF_TIME_LIMIT = None

    @staticmethod
    def init_app(app):
        pass
