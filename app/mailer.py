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
    """Best-effort delivery; returns 'sent' / 'skipped' / 'failed'.

    SMTP configuration comes from the Settings page with env as fallback
    (:func:`app.settings.value`), so changes apply without a restart.
    """
    from . import settings
    if not to:
        return "skipped"
    if current_app.config.get("TESTING"):
        OUTBOX.append((to, subject, body))
        return "sent"
    server = settings.value("mail_server")
    if not server:
        return "skipped"
    try:
        msg = EmailMessage()
        msg["From"] = settings.value("mail_default_sender") or "biggy@localhost"
        msg["To"] = to
        msg["Subject"] = subject or ""
        msg.set_content(body or "")
        with smtplib.SMTP(server, settings.value("mail_port") or 25,
                          timeout=settings.value("notify_webhook_timeout") or 5) as s:
            if settings.value("mail_use_tls"):
                s.starttls()
            if settings.value("mail_username"):
                s.login(settings.value("mail_username"),
                        settings.value("mail_password") or "")
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
    from . import settings
    return (settings.value("base_url") or "").rstrip("/")
