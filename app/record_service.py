"""Metadata-aware CRUD: audit stamping, soft-delete, row ownership, change log.

Wraps the generic :mod:`app.data_service` so that module stays free of policy
(and untouched for importer/schema_io/data_io). User-mode routes call this.
"""
import json
from datetime import date, datetime, timezone

from sqlalchemy import select

from . import data_service
from .identifiers import junction_name
from .metadata.models import AuditLog, MetaField, MetaRelation, MetaTable

_MANAGED = {"created_by", "created_at", "updated_by", "updated_at", "deleted_at", "deleted_by"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _has_audit_cols(meta_table):
    return meta_table.track_audit or meta_table.row_owned


def _log(session, meta_table, pk, action, user_id, changes=None):
    if not meta_table.track_audit:
        return
    session.add(AuditLog(
        table_phys=meta_table.phys_name, row_pk=pk, action=action, user_id=user_id,
        changes=json.dumps(changes, default=str) if changes else None,
    ))
    session.commit()


# --------------------------------------------------------------------------- #
# Mutations
# --------------------------------------------------------------------------- #
def _has_triggers(session, meta_table):
    from . import triggers
    return triggers.has_rules(session, meta_table.id)


def _fire(session, engine, meta_table, event, pk, old_row, user_id):
    from . import triggers
    try:
        triggers.fire(session, engine, meta_table, event, pk, old_row, user_id)
    except Exception:  # noqa: BLE001 - triggers must never break the write
        pass
    from . import feeds
    try:
        feeds.run_for_event(session, engine, meta_table, event, pk, old_row, user_id)
    except Exception:  # noqa: BLE001 - feeds must never break the write
        pass
    from . import sla
    try:
        sla.run_for_event(session, engine, meta_table, event, pk, old_row, user_id)
    except Exception:  # noqa: BLE001 - SLA must never break the write
        pass


_DEFAULT_TOKENS = {"now", "today", "current_user", "me"}


def _apply_generated(session, meta_table, values, user_id):
    """Fill auto-number fields and default-expression tokens not already provided."""
    for f in meta_table.fields:
        if values.get(f.phys_name) not in (None, ""):
            continue
        if f.data_type == "autonumber":
            values[f.phys_name] = _next_autonumber(session, f)
        elif (f.default_value or "").strip().lower() in _DEFAULT_TOKENS:
            v = _eval_default_token(session, f.default_value, user_id)
            if v is not None:
                values[f.phys_name] = v


def _next_autonumber(session, field):
    from .metadata.models import Sequence
    seq = session.scalar(select(Sequence).where(Sequence.field_id == field.id))
    if not seq:
        seq = Sequence(field_id=field.id, next=1)
        session.add(seq)
        session.flush()
    n = seq.next
    seq.next = n + 1
    session.commit()
    return f"{field.default_value or ''}{n:04d}"


def _eval_default_token(session, default_value, user_id):
    from .metadata.models import AppUser
    tok = (default_value or "").strip().lower()
    if tok == "now":
        return _now()
    if tok == "today":
        return date.today()
    if tok in ("current_user", "me"):
        u = session.get(AppUser, user_id) if user_id else None
        return u.username if u else None
    return None


def _has_formulas(meta_table):
    return any(f.data_type == "formula" for f in meta_table.fields)


def _compute_self(session, engine, meta_table, ctx, pk):
    """Computed formula columns for one record (best-effort)."""
    from . import formula
    try:
        return formula.compute_values(session, engine, meta_table, ctx, pk)
    except Exception:  # noqa: BLE001 - a formula must never break the write
        return {}


def _ripple(session, engine, meta_table, row):
    """Recompute formula columns of records related to ``row`` (best-effort)."""
    from . import formula
    try:
        formula.recompute_related(session, engine, meta_table, row)
    except Exception:  # noqa: BLE001
        pass


def create(session, engine, meta_table, values, user_id):
    values = dict(values)
    _apply_generated(session, meta_table, values, user_id)
    if _has_formulas(meta_table):
        values.update(_compute_self(session, engine, meta_table, values, None))
    if _has_audit_cols(meta_table):
        now = _now()
        values.update(created_by=user_id, created_at=now, updated_by=user_id, updated_at=now)
    pk = data_service.insert_row(engine, meta_table.phys_name, values)
    changes = ({k: [None, v] for k, v in values.items() if k not in _MANAGED}
               if meta_table.track_audit else None)
    _log(session, meta_table, pk, "create", user_id, changes)
    _fire(session, engine, meta_table, "create", pk, None, user_id)
    _ripple(session, engine, meta_table, {**values, meta_table.pk_col: pk})
    return pk


def update(session, engine, meta_table, pk, values, user_id):
    values = dict(values)
    changes, old = None, None
    if meta_table.track_audit or _has_triggers(session, meta_table) or _has_formulas(meta_table):
        old = data_service.get_row(engine, meta_table.phys_name, pk) or {}
    if _has_formulas(meta_table):
        values.update(_compute_self(session, engine, meta_table, {**old, **values}, pk))
    if meta_table.track_audit:
        changes = {k: [old.get(k), v] for k, v in values.items() if old.get(k) != v}
    if _has_audit_cols(meta_table):
        values.update(updated_by=user_id, updated_at=_now())
    data_service.update_row(engine, meta_table.phys_name, pk, values)
    _log(session, meta_table, pk, "update", user_id, changes)
    _fire(session, engine, meta_table, "update", pk, old, user_id)
    _ripple(session, engine, meta_table, data_service.get_row(engine, meta_table.phys_name, pk)
            or {**old, **values, meta_table.pk_col: pk})


def _incoming_m1(session, meta_table):
    """(child_table, fk_col, relation) for every M:1 pointing at this table."""
    out = []
    for rel in session.scalars(select(MetaRelation).where(
            MetaRelation.kind == "m1", MetaRelation.to_table_id == meta_table.id)):
        child = session.get(MetaTable, rel.from_table_id)
        fk = session.get(MetaField, rel.from_field_id) if rel.from_field_id else None
        if child and fk:
            out.append((child, fk.phys_name, rel))
    return out


def _mn_links(session, meta_table):
    """Per-direction M:N links: (relation, junction, this_col, other_col, other_table)."""
    out = []
    for rel in session.scalars(select(MetaRelation).where(MetaRelation.kind == "mn")):
        a = session.get(MetaTable, rel.from_table_id)
        b = session.get(MetaTable, rel.to_table_id)
        if not a or not b or meta_table.id not in (a.id, b.id):
            continue
        jname = junction_name(a.phys_name, b.phys_name)
        left, right = f"{a.phys_name}_id", f"{b.phys_name}_id"
        if left == right:
            right = f"{b.phys_name}_id_2"
        if a.id == meta_table.id:
            out.append((rel, jname, left, right, b))
        if b.id == meta_table.id:
            out.append((rel, jname, right, left, a))
    return out


def _display(session, meta_table):
    from .forms.builder import display_field_name
    return display_field_name(session, meta_table)


def _purge_attachments(session, meta_table, pk):
    """Delete a row's file/image attachments + their files (hard delete only)."""
    from . import file_store
    from .metadata.field_types import FILE_TYPES
    from .metadata.models import Attachment

    field_ids = [f.id for f in meta_table.fields if f.data_type in FILE_TYPES]
    if not field_ids:
        return
    for att in session.scalars(select(Attachment).where(
            Attachment.field_id.in_(field_ids), Attachment.row_pk == pk)):
        file_store.delete(att.field_id, att.stored_name)
        session.delete(att)
    session.commit()


_SAMPLE = 25


def delete_impact(session, engine, meta_table, pk, *, hard):
    """Summarise the related objects affected by deleting this row, with samples."""
    children = []
    for child, fk, rel in _incoming_m1(session, meta_table):
        count = data_service.count_rows(engine, child.phys_name, fk, pk)
        if not count:
            continue
        if hard:
            action = (rel.on_delete or "SET NULL").upper()
        else:
            action = "set NULL" if data_service.column_nullable(engine, child.phys_name, fk) \
                else "delete"
        samples = data_service.sample_labels(engine, child.phys_name, fk, pk,
                                             [_display(session, child)], _SAMPLE)
        children.append({"label": child.label, "count": count, "action": action,
                         "samples": samples})

    by_rel = {}
    for rel, jname, this_col, other_col, other in _mn_links(session, meta_table):
        count = data_service.count_rows(engine, jname, this_col, pk)
        if not count:
            continue
        other_ids = data_service.get_links(engine, jname, this_col, pk, other_col)
        labels = data_service.labels_for(engine, other.phys_name, other_ids[:_SAMPLE],
                                         [_display(session, other)])
        entry = by_rel.setdefault(rel.id,
                                  {"label": rel.name or other.label, "count": 0, "samples": []})
        entry["count"] += count
        entry["samples"] += labels
    links = [{**e, "samples": e["samples"][:_SAMPLE]} for e in by_rel.values()]

    blocked = hard and any(c["action"] == "RESTRICT" for c in children)
    return {"children": children, "links": links, "blocked": blocked}


def remove(session, engine, meta_table, pk, user_id):
    """Soft-delete (Trash) when enabled, otherwise hard delete."""
    # fetch the row unconditionally: triggers, formula ripple to related tables,
    # and dissociation below may all need it.
    old = data_service.get_row(engine, meta_table.phys_name, pk)
    if meta_table.soft_delete:
        # dissociate: drop M:N links and clear/remove incoming references
        for rel, jname, this_col, other_col, other in _mn_links(session, meta_table):
            data_service.delete_where(engine, jname, this_col, pk)
        for child, fk, rel in _incoming_m1(session, meta_table):
            if data_service.column_nullable(engine, child.phys_name, fk):
                data_service.clear_fk(engine, child.phys_name, fk, pk)
            else:
                data_service.delete_where(engine, child.phys_name, fk, pk)
        data_service.update_row(engine, meta_table.phys_name, pk,
                                {"deleted_at": _now(), "deleted_by": user_id})
    else:
        data_service.delete_row(engine, meta_table.phys_name, pk)
        _purge_attachments(session, meta_table, pk)
    _log(session, meta_table, pk, "delete", user_id)
    _fire(session, engine, meta_table, "delete", pk, old, user_id)
    _ripple(session, engine, meta_table, old)


def restore(session, engine, meta_table, pk, user_id):
    data_service.update_row(engine, meta_table.phys_name, pk,
                            {"deleted_at": None, "deleted_by": None})
    _log(session, meta_table, pk, "restore", user_id)


def destroy(session, engine, meta_table, pk, user_id):
    """Permanently delete (used from Trash)."""
    data_service.delete_row(engine, meta_table.phys_name, pk)
    _purge_attachments(session, meta_table, pk)
    _log(session, meta_table, pk, "delete", user_id)


# --------------------------------------------------------------------------- #
# Reads (scoped by soft-delete + ownership)
# --------------------------------------------------------------------------- #
def _scope_filters(meta_table, *, user_id, is_designer, include_deleted):
    extra = []
    if meta_table.soft_delete:
        extra.append({"col": "deleted_at", "op": "not_empty" if include_deleted else "empty"})
    if meta_table.row_owned and not is_designer:
        extra.append({"col": "created_by", "op": "eq", "value": user_id})
    return extra


def list_records(engine, meta_table, *, user_id, is_designer, include_deleted=False,
                 filters=None, **kw):
    scoped = (filters or []) + _scope_filters(
        meta_table, user_id=user_id, is_designer=is_designer, include_deleted=include_deleted)
    return data_service.list_rows(engine, meta_table.phys_name, filters=scoped, **kw)


def get_record(engine, meta_table, pk, *, user_id, is_designer, allow_deleted=False):
    row = data_service.get_row(engine, meta_table.phys_name, pk)
    if not row:
        return None
    if meta_table.soft_delete and not allow_deleted and row.get("deleted_at") is not None:
        return None
    if meta_table.row_owned and not is_designer and row.get("created_by") not in (None, user_id):
        return None
    return row
