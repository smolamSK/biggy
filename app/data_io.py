"""Export and import all row data of the managed tables as JSON.

The data counterpart to :mod:`app.schema_io`. Import is a *restore*: it
preserves row ids (so foreign keys stay valid) and, with ``replace``, clears the
current rows first. Only app-managed tables and M:N junctions are touched —
never the ``app_*`` metadata or ``app_user`` tables.
"""
from sqlalchemy import insert, select, text

from . import data_service
from .metadata import ddl
from .metadata.models import MetaRelation, MetaTable

DATA_VERSION = 1


class DataError(Exception):
    """Raised for an invalid data import payload."""


def _table_engines(session, engine, managed_only=False):
    """``{phys_name: engine}`` for tables + M:N junctions, each in its data source.

    With ``managed_only`` (used on import), adopted external tables are excluded —
    a restore must never clear or overwrite rows in a database Biggy doesn't own.
    """
    from .db import engine_for_table
    out = {}
    for t in session.scalars(select(MetaTable).order_by(MetaTable.id)):
        if t.managed or not managed_only:
            out[t.phys_name] = engine_for_table(t)
    for r in session.scalars(select(MetaRelation).where(MetaRelation.kind == "mn")):
        if r.junction_phys_name:
            ft = session.get(MetaTable, r.from_table_id)
            out[r.junction_phys_name] = engine_for_table(ft) if ft else engine
    return out


def export_data(session, engine):
    """Serialise every row of every table to a JSON-able dict (each via its source)."""
    out = {"version": DATA_VERSION, "tables": {}}
    for name, eng in _table_engines(session, engine).items():
        table = data_service.reflect_table(eng, name)
        with eng.connect() as conn:
            out["tables"][name] = [dict(r) for r in conn.execute(select(table)).mappings()]
    return out


def import_data(session, engine, data, replace=True):
    """Restore rows from an exported payload (per data source). Returns a summary."""
    if not isinstance(data, dict) or data.get("version") != DATA_VERSION:
        raise DataError("Unsupported or missing data version.")
    payload = data.get("tables") or {}
    if not isinstance(payload, dict):
        raise DataError("Malformed data payload.")

    name_engines = _table_engines(session, engine, managed_only=True)
    known_set = set(name_engines)
    targets = {name: rows for name, rows in payload.items() if name in known_set}
    skipped = [name for name in payload if name not in known_set]

    # group both the clear-list and the insert-list by data-source engine
    clear_by, insert_by, engines = {}, {}, {}
    for name, eng in name_engines.items():
        engines[id(eng)] = eng
        clear_by.setdefault(id(eng), []).append(name)
    for name, rows in targets.items():
        insert_by.setdefault(id(name_engines[name]), []).append((name, rows))

    rows_total = 0
    for ekey, eng in engines.items():
        q = eng.dialect.identifier_preparer.quote
        with eng.begin() as conn:                  # atomic per data source
            with ddl.fk_disabled(conn):
                if replace:
                    for name in clear_by.get(ekey, []):
                        conn.execute(text(f"DELETE FROM {q(name)}"))
                for name, rows in insert_by.get(ekey, []):
                    if not rows:
                        continue
                    table = data_service.reflect_table(eng, name)
                    cols = set(table.c.keys())
                    cleaned = [{k: v for k, v in row.items() if k in cols} for row in rows]
                    conn.execute(insert(table), cleaned)   # id included → preserves references
                    rows_total += len(cleaned)

    return {"tables": len(targets), "rows": rows_total, "skipped": skipped}
