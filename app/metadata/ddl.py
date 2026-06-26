"""Dialect-agnostic DDL helpers.

Wraps Alembic's :class:`~alembic.operations.Operations` (used standalone, not as a
migration framework) so the schema operations in :mod:`app.metadata.schema_service`
run on MariaDB/MySQL, PostgreSQL and SQLite alike. Alembic renders ``ALTER`` per
dialect and — via ``batch_alter_table`` — rebuilds the table on SQLite, which
cannot alter/drop columns in place.
"""
from contextlib import contextmanager

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, text


def operations(conn):
    """An Alembic ``Operations`` bound to an open connection."""
    return Operations(MigrationContext.configure(conn))


@contextmanager
def fk_disabled(conn):
    """Disable foreign-key enforcement for the duration (per dialect).

    Used for bulk drops/deletes that aren't in FK order. SQLite does not enforce
    FKs by default (the PRAGMA is also a no-op inside a transaction), and the
    PostgreSQL trick needs privileges — both are best-effort.
    """
    name = conn.dialect.name
    try:
        if name in ("mysql", "mariadb"):
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        elif name == "sqlite":
            conn.execute(text("PRAGMA foreign_keys=OFF"))
        elif name == "postgresql":
            try:
                conn.execute(text("SET session_replication_role = replica"))
            except Exception:  # noqa: BLE001 - needs superuser; proceed anyway
                pass
        yield
    finally:
        if name in ("mysql", "mariadb"):
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        elif name == "sqlite":
            conn.execute(text("PRAGMA foreign_keys=ON"))
        elif name == "postgresql":
            try:
                conn.execute(text("SET session_replication_role = DEFAULT"))
            except Exception:  # noqa: BLE001
                pass


def add_column_if_missing(engine, table_name, column):
    """``ALTER TABLE ADD COLUMN`` only when the column isn't already present.

    Replaces the MySQL-only ``ADD COLUMN IF NOT EXISTS``; portable via reflection.
    """
    existing = {c["name"] for c in inspect(engine).get_columns(table_name)}
    if column.name in existing:
        return
    with engine.begin() as conn:
        operations(conn).add_column(table_name, column)
