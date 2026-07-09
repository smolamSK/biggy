"""Maintenance windows: planned-work periods that hold the alarm machinery.

While a window is **active** for a table (its own window or a global one):
SLA breach marking and escalation are postponed until the window ends, and
trigger/watch notification channels are suppressed (data actions like
``set_field`` still run). Conversations are never muted — humans keep talking.
A window may link to the change record that motivates it.
"""
from datetime import datetime, timezone

from sqlalchemy import or_, select

from .metadata.models import MaintenanceWindow


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_active(session, meta_table, at=None):
    """The active window covering this table (or all tables), else None."""
    now = at or _now()
    return session.scalar(select(MaintenanceWindow).where(
        MaintenanceWindow.starts_at <= now, MaintenanceWindow.ends_at > now,
        or_(MaintenanceWindow.table_id.is_(None),
            MaintenanceWindow.table_id == meta_table.id))
        .order_by(MaintenanceWindow.ends_at.desc()))


def for_record(session, table_id, pk):
    """Windows linked to a record, newest first — its record-page panel."""
    return session.scalars(select(MaintenanceWindow).where(
        MaintenanceWindow.record_table_id == table_id,
        MaintenanceWindow.record_pk == str(pk))
        .order_by(MaintenanceWindow.starts_at.desc())).all()


def status(window, at=None):
    now = at or _now()
    if now < window.starts_at:
        return "upcoming"
    if now < window.ends_at:
        return "active"
    return "past"
