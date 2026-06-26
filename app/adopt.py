"""Adopt existing (non-Biggy) database tables by introspection.

Reflects tables already present in the connected database and generates Biggy
metadata (``MetaTable``/``MetaField``/``MetaRelation``) mapped onto them, marked
``managed=False`` so Biggy never issues DDL against them. This is what lets Biggy
act as a front-end (forms/views/workflows) over a database it did not create.

v1 adopts tables whose primary key is a single integer column named ``id`` (the
framework default). The reverse type map mirrors
:func:`app.metadata.schema_service.sa_type_for_field`.
"""
import json

import sqlalchemy.types as st
from sqlalchemy import inspect, select

from .identifiers import IdentifierError, validate_identifier
from .metadata.field_types import RELATION_TYPE
from .metadata.models import MetaField, MetaRelation, MetaTable


def field_attrs_for(col):
    """Map a reflected column to Biggy field attributes (``data_type`` + extras)."""
    t = col["type"]
    if isinstance(t, st.Boolean):
        return {"data_type": "boolean"}
    if isinstance(t, st.Enum):
        opts = list(getattr(t, "enums", []) or [])
        return {"data_type": "enum", "enum_options": json.dumps(opts) if opts else None}
    if isinstance(t, st.JSON):
        return {"data_type": "json"}
    if isinstance(t, st.DateTime):
        return {"data_type": "datetime"}
    if isinstance(t, st.Date):
        return {"data_type": "date"}
    if isinstance(t, st.Time):
        return {"data_type": "time"}
    if isinstance(t, st.BigInteger):
        return {"data_type": "bigint"}
    if isinstance(t, st.Integer):
        return {"data_type": "integer"}
    if isinstance(t, st.Float):
        return {"data_type": "float"}
    if isinstance(t, st.Numeric):
        return {"data_type": "decimal", "precision": t.precision, "scale": t.scale}
    if isinstance(t, st.Text):
        return {"data_type": "text"}
    if isinstance(t, st.String):
        return {"data_type": "string", "length": t.length}
    return {"data_type": "string"}            # unknown → treat as text-ish


def _pk_reason(insp, name):
    """Return ``None`` if the table has a single integer ``id`` PK, else a reason."""
    return _pk_info(insp, name)[2]


def _pk_info(insp, name):
    """Return ``(pk_col, is_autoincrement, reason)`` for a table's single PK."""
    cols = insp.get_pk_constraint(name).get("constrained_columns") or []
    if len(cols) != 1:
        return None, False, ("needs a single-column primary key"
                             if not cols else "composite primary keys aren't supported")
    pk_col = cols[0]
    col = next((c for c in insp.get_columns(name) if c["name"] == pk_col), None)
    is_int = bool(col) and isinstance(col["type"], st.Integer)
    auto = is_int and (col.get("autoincrement") in (True, "auto") or pk_col == "id")
    return pk_col, auto, None


def _excluded(session):
    """phys names already mapped + M:N junctions (never adoptable)."""
    mapped = {t.phys_name for t in session.scalars(select(MetaTable))}
    mapped |= {r.junction_phys_name for r in
               session.scalars(select(MetaRelation).where(MetaRelation.kind == "mn"))
               if r.junction_phys_name}
    return mapped


def list_adoptable(session, engine):
    """Return ``[{name, ok, reason, ncols}]`` for every candidate table."""
    insp = inspect(engine)
    excluded = _excluded(session)
    out = []
    for name in sorted(insp.get_table_names()):
        if name.startswith("app_") or name in excluded:
            continue
        try:
            validate_identifier(name, kind="Table")
        except IdentifierError:
            out.append({"name": name, "ok": False, "reason": "name is not a valid identifier"})
            continue
        reason = _pk_reason(insp, name)
        out.append({"name": name, "ok": reason is None, "reason": reason,
                    "ncols": len(insp.get_columns(name))})
    return out


def adopt_table(session, engine, phys, label=None, source_id=None):
    """Map an existing table into metadata (``managed=False``). Returns ``(mt, error)``."""
    insp = inspect(engine)
    pk_col, pk_auto, reason = _pk_info(insp, phys)
    if reason:
        return None, reason
    if session.scalar(select(MetaTable).where(MetaTable.phys_name == phys)):
        return None, "already mapped"

    mt = MetaTable(phys_name=phys, label=label or phys.replace("_", " ").title(),
                   managed=False, source_id=source_id, pk_col=pk_col)
    session.add(mt)
    session.flush()

    fk_cols = {c for fk in insp.get_foreign_keys(phys)
               for c in (fk.get("constrained_columns") or [])}
    display_id, pos = None, 0
    for col in insp.get_columns(phys):
        cname = col["name"]
        if cname in fk_cols:                          # FKs → relations
            continue
        if cname == pk_col and pk_auto:               # auto-increment key is implicit
            continue
        try:
            validate_identifier(cname, kind="Column")
        except IdentifierError:
            continue
        attrs = field_attrs_for(col)
        is_pk = cname == pk_col                        # a natural key: required + unique
        mf = MetaField(table_id=mt.id, phys_name=cname,
                       label=cname.replace("_", " ").title(), is_unique=is_pk,
                       nullable=bool(col.get("nullable", True)) and not is_pk,
                       position=pos, **attrs)
        session.add(mf)
        session.flush()
        pos += 1
        if display_id is None and attrs["data_type"] in ("string", "text"):
            display_id = mf.id
    if display_id:
        mt.display_field_id = display_id
    session.flush()
    return mt, None


def adopt_relations(session, engine, source_id=None):
    """Create M:1 relations from foreign keys between adopted tables in one source."""
    insp = inspect(engine)
    external = {t.phys_name: t for t in session.scalars(select(MetaTable))
               if not t.managed and t.source_id == source_id}
    created = 0
    for phys, mt in external.items():
        existing = {f.phys_name for f in mt.fields}
        for fk in insp.get_foreign_keys(phys):
            cols = fk.get("constrained_columns") or []
            ref = fk.get("referred_table")
            if len(cols) != 1 or ref not in external or cols[0] in existing:
                continue
            fkcol = cols[0]
            try:
                validate_identifier(fkcol, kind="Column")
            except IdentifierError:
                continue
            target = external[ref]
            on_delete = (fk.get("options") or {}).get("ondelete") or "RESTRICT"
            mf = MetaField(table_id=mt.id, phys_name=fkcol,
                           label=fkcol.replace("_", " ").title(), data_type=RELATION_TYPE,
                           related_table_id=target.id, on_delete=on_delete,
                           nullable=True, position=len(mt.fields))
            session.add(mf)
            session.flush()
            session.add(MetaRelation(name=f"{phys}.{fkcol}", kind="m1",
                                     from_table_id=mt.id, to_table_id=target.id,
                                     from_field_id=mf.id, on_delete=on_delete))
            existing.add(fkcol)
            created += 1
    session.flush()
    return created
