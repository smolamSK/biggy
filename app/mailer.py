"""Outbound email for person-centric notifications (comments, watch, assign).

Uses the same ``MAIL_*`` configuration as trigger emails. Delivery is
best-effort: failures are logged, never raised — email must not break a write.
Under TESTING nothing is sent; messages append to :data:`OUTBOX` so tests can
assert deliveries.
"""
import logging
import smtplib
from email.message import EmailMessage

from flask import current_app

_logger = logging.getLogger(__name__)

OUTBOX = []      # (to, subject, body) tuples — TESTING only


def send(to, subject, body):
    """Best-effort delivery; returns 'sent' / 'skipped' / 'failed'."""
    cfg = current_app.config
    if not to:
        return "skipped"
    if cfg.get("TESTING"):
        OUTBOX.append((to, subject, body))
        return "sent"
    if not cfg.get("MAIL_SERVER"):
        return "skipped"
    try:
        msg = EmailMessage()
        msg["From"] = cfg.get("MAIL_DEFAULT_SENDER", "biggy@localhost")
        msg["To"] = to
        msg["Subject"] = subject or ""
        msg.set_content(body or "")
        with smtplib.SMTP(cfg["MAIL_SERVER"], cfg.get("MAIL_PORT", 25),
                          timeout=cfg.get("NOTIFY_WEBHOOK_TIMEOUT", 5)) as s:
            if cfg.get("MAIL_USE_TLS"):
                s.starttls()
            if cfg.get("MAIL_USERNAME"):
                s.login(cfg["MAIL_USERNAME"], cfg.get("MAIL_PASSWORD", ""))
            s.send_message(msg)
        return "sent"
    except Exception as exc:  # noqa: BLE001 - email must never break a write
        _logger.warning("email to %s failed: %s", to, exc)
        return "failed"


def email_user(user, subject, body):
    """Send to an app user when they can and want to receive email."""
    if user is None or not getattr(user, "is_active", True):
        return "skipped"
    if not getattr(user, "email", None) or not getattr(user, "notify_email", True):
        return "skipped"
    return send(user.email, subject, body)


def base_url():
    """The instance's public URL for links in emails ('' = no links)."""
    from .db import SessionLocal
    from .settings import get_all
    try:
        stored = get_all(SessionLocal()).get("base_url", "")
    except Exception:  # noqa: BLE001
        stored = ""
    return (stored or current_app.config.get("APP_BASE_URL", "")).rstrip("/")
