"""Instance settings — UI-editable, with env/``Config`` values as fallback.

Stored in the ``app_setting`` key-value table so adding a setting never needs a
migration. :data:`REGISTRY` declares every editable key (section, type, the
``Config`` attribute it falls back to, and whether it's a secret — secrets are
Fernet-encrypted inside the value). :func:`value` is the one runtime getter the
rest of the app consumes: stored-and-non-blank wins, else the env fallback —
so operators may keep using ``.env``, and anything set on the Settings page
takes effect immediately, no restart.

Deliberately *not* here: bootstrap/process-level configuration (``SECRET_KEY``,
``BIGGY_ENCRYPTION_KEY``, ``DB_*``, upload folder, log level, scheduler ticker,
session cookies) — those stay environment-only and the Settings page shows the
restart-bound ones read-only.
"""
from flask import current_app, g
from sqlalchemy import select

from .metadata.models import AppSetting

THEMES = ("light", "dark", "sepia", "ocean", "contrast")

#: the settings the Designer-mode Settings page edits
BRANDING_KEYS = ("app_name", "accent", "default_theme")

# key → spec: section (page grouping), type (str|int|bool), config (Config
# attribute fallback), secret (encrypted at rest), label/help for the page.
REGISTRY = {
    # --- branding ---------------------------------------------------------
    "app_name": {"section": "branding", "type": "str", "config": "APP_NAME"},
    "accent": {"section": "branding", "type": "str", "config": None},
    "default_theme": {"section": "branding", "type": "str", "config": None},
    "base_url": {"section": "branding", "type": "str", "config": "APP_BASE_URL"},
    # --- email (SMTP) ------------------------------------------------------
    "mail_server": {"section": "email", "type": "str", "config": "MAIL_SERVER",
                    "label": "SMTP server"},
    "mail_port": {"section": "email", "type": "int", "config": "MAIL_PORT",
                  "label": "Port"},
    "mail_use_tls": {"section": "email", "type": "bool", "config": "MAIL_USE_TLS",
                     "label": "Use TLS (STARTTLS)"},
    "mail_username": {"section": "email", "type": "str", "config": "MAIL_USERNAME",
                      "label": "Username"},
    "mail_password": {"section": "email", "type": "str", "config": "MAIL_PASSWORD",
                      "secret": True, "label": "Password"},
    "mail_default_sender": {"section": "email", "type": "str",
                            "config": "MAIL_DEFAULT_SENDER", "label": "Sender address"},
    # --- SSO (OpenID Connect) ----------------------------------------------
    "oidc_issuer": {"section": "sso", "type": "str", "config": "OIDC_ISSUER",
                    "label": "Issuer URL"},
    "oidc_client_id": {"section": "sso", "type": "str", "config": "OIDC_CLIENT_ID",
                       "label": "Client id"},
    "oidc_client_secret": {"section": "sso", "type": "str",
                           "config": "OIDC_CLIENT_SECRET", "secret": True,
                           "label": "Client secret"},
    "oidc_scopes": {"section": "sso", "type": "str", "config": "OIDC_SCOPES",
                    "label": "Scopes"},
    "oidc_username_claim": {"section": "sso", "type": "str",
                            "config": "OIDC_USERNAME_CLAIM", "label": "Username claim"},
    "oidc_provision": {"section": "sso", "type": "str", "config": "OIDC_PROVISION",
                       "label": "Provisioning (link | jit)"},
    "oidc_default_role": {"section": "sso", "type": "str",
                          "config": "OIDC_DEFAULT_ROLE", "label": "JIT default role"},
    "oidc_button_label": {"section": "sso", "type": "str",
                          "config": "OIDC_BUTTON_LABEL", "label": "Button label"},
    "oidc_redirect_uri": {"section": "sso", "type": "str",
                          "config": "OIDC_REDIRECT_URI", "label": "Redirect URI"},
    # --- sign-in policy -----------------------------------------------------
    "require_mfa": {"section": "signin", "type": "bool", "config": "REQUIRE_MFA",
                    "label": "Require two-factor for everyone"},
    "login_rate_limit": {"section": "signin", "type": "int",
                         "config": "LOGIN_RATE_LIMIT",
                         "label": "Login lockout: failed attempts (0 = off)"},
    "login_rate_window": {"section": "signin", "type": "int",
                          "config": "LOGIN_RATE_WINDOW",
                          "label": "Login lockout: window (seconds)"},
    # --- limits & defaults --------------------------------------------------
    "webhook_max_body_bytes": {"section": "limits", "type": "int",
                               "config": "WEBHOOK_MAX_BODY_BYTES",
                               "label": "Webhook max body (bytes)"},
    "webhook_rate_limit": {"section": "limits", "type": "int",
                           "config": "WEBHOOK_RATE_LIMIT",
                           "label": "Webhook rate limit (per window, 0 = off)"},
    "webhook_rate_window": {"section": "limits", "type": "int",
                            "config": "WEBHOOK_RATE_WINDOW",
                            "label": "Webhook rate window (seconds)"},
    "topology_default_depth": {"section": "limits", "type": "int",
                               "config": "TOPOLOGY_DEFAULT_DEPTH",
                               "label": "Impact map: default depth"},
    "topology_max_depth": {"section": "limits", "type": "int",
                           "config": "TOPOLOGY_MAX_DEPTH",
                           "label": "Impact map: max depth"},
    "topology_max_nodes": {"section": "limits", "type": "int",
                           "config": "TOPOLOGY_MAX_NODES",
                           "label": "Impact map: max nodes"},
    "sla_default_warn_minutes": {"section": "limits", "type": "int",
                                 "config": "SLA_DEFAULT_WARN_MINUTES",
                                 "label": "SLA: default warn minutes"},
    "notify_webhook_timeout": {"section": "limits", "type": "int",
                               "config": "NOTIFY_WEBHOOK_TIMEOUT",
                               "label": "Outbound email/webhook timeout (s)"},
    "currency_symbol": {"section": "limits", "type": "str",
                        "config": "CURRENCY_SYMBOL", "label": "Currency symbol"},
}

_TRUE = {"1", "true", "yes", "on"}


def get_all(session):
    """All stored settings as a raw dict (blank values omitted)."""
    return {s.key: s.value for s in session.scalars(select(AppSetting))
            if s.value not in (None, "")}


def save(session, mapping):
    """Upsert the given settings; blank values delete the row (fall back).

    Secret keys are encrypted before storage.
    """
    from . import crypto
    existing = {s.key: s for s in session.scalars(select(AppSetting))}
    for key, value_ in mapping.items():
        value_ = (value_ or "").strip()
        if value_ and REGISTRY.get(key, {}).get("secret"):
            value_ = crypto.encrypt(value_)
        row = existing.get(key)
        if not value_:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(AppSetting(key=key, value=value_))
        else:
            row.value = value_
    session.commit()
    try:
        g.pop("_app_settings", None)      # bust the per-request cache
    except RuntimeError:
        pass


def _stored():
    """Per-request cache of the raw stored values."""
    try:
        cache = getattr(g, "_app_settings", None)
    except RuntimeError:                  # outside a request/app context
        cache = None
        g_ok = False
    else:
        g_ok = True
    if cache is None:
        from .db import SessionLocal
        try:
            cache = get_all(SessionLocal())
        except Exception:  # noqa: BLE001 - settings must never break a request
            cache = {}
        if g_ok:
            g._app_settings = cache
    return cache


def _coerce(kind, raw):
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if kind == "bool":
        return str(raw).strip().lower() in _TRUE
    return raw


def value(key):
    """The effective setting: stored (decrypted, typed) over the env fallback."""
    spec = REGISTRY[key]
    raw = _stored().get(key)
    if raw not in (None, ""):
        if spec.get("secret"):
            from . import crypto
            try:
                raw = crypto.decrypt(raw)
            except Exception:  # noqa: BLE001 - undecryptable (rotated key)
                raw = ""
        coerced = _coerce(spec["type"], raw)
        if coerced is not None and coerced != "":
            return coerced                # note: a stored bool "0" → False here,
                                          # so an explicit off beats the env
    if spec.get("config"):
        return current_app.config.get(spec["config"])
    return None


def oidc_enabled():
    return bool(value("oidc_issuer") and value("oidc_client_id"))


def branding():
    """Branding values for templates: stored settings over Config defaults."""
    out = {
        "app_name": current_app.config.get("APP_NAME", "Biggy"),
        "accent": "",
        "default_theme": "",
    }
    stored = _stored()
    for key in BRANDING_KEYS:
        if stored.get(key):
            out[key] = stored[key]
    if out["default_theme"] not in THEMES:
        out["default_theme"] = ""
    return out
