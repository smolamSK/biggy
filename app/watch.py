"""Record watching: subscribe to a data record, get notified on every change.

Watchers receive an in-app notification when the record is updated through the
:mod:`app.record_service` chokepoint (any write path: form, inline edit, API,
webhook, kanban) and when someone comments (via :mod:`app.comments`). System
recomputes (formula ripple, SLA write-backs) go through the low-level
``data_service`` and deliberately do not notify.
"""
from sqlalchemy import select

from . import mailer
from .metadata.models import AppUser, Notification, Watch

_AUDIT_COLS = {"created_by", "created_at", "updated_by", "updated_at",
               "deleted_by", "deleted_at"}


def is_watching(session, user_id, table_phys, pk):
    return session.scalar(select(Watch.id).where(
        Watch.user_id == user_id, Watch.table_phys == table_phys,
        Watch.row_pk == str(pk))) is not None


def toggle(session, user_id, table_phys, pk):
    """Flip the subscription; returns True when now watching."""
    row = session.scalar(select(Watch).where(
        Watch.user_id == user_id, Watch.table_phys == table_phys,
        Watch.row_pk == str(pk)))
    if row is not None:
        session.delete(row)
        session.commit()
        return False
    session.add(Watch(user_id=user_id, table_phys=table_phys, row_pk=str(pk)))
    session.commit()
    return True


def watchers(session, table_phys, pk):
    """User ids subscribed to the record."""
    return set(session.scalars(select(Watch.user_id).where(
        Watch.table_phys == table_phys, Watch.row_pk == str(pk))).all())


def notify_update(session, meta_table, pk, values, user_id):
    """In-app notification to the record's watchers (except the actor)."""
    ids = watchers(session, meta_table.phys_name, pk) - {user_id}
    if not ids:
        return
    changed = sorted(k for k in values if k not in _AUDIT_COLS)
    actor = session.get(AppUser, user_id) if user_id else None
    body = (f"{actor.username if actor else 'someone'} changed: "
            + (", ".join(changed) if changed else "record"))
    try:
        int_pk = int(pk)
    except (TypeError, ValueError):
        int_pk = None
    base = mailer.base_url()
    link = f"\n\n{base}/u/view/{meta_table.id}/{pk}" if base else ""
    subject = f"{meta_table.label} #{pk} was updated"
    for uid in ids:
        u = session.get(AppUser, uid)
        if u is None or not u.is_active:
            continue
        session.add(Notification(
            table_phys=meta_table.phys_name, row_pk=int_pk, event="watch",
            channel="in_app", user_id=uid, subject=subject, body=body,
            status="unread"))
        mailer.email_user(u, subject, body + link)
    session.commit()
