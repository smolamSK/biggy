"""Shared table, field, row and attachment authorization policies."""
from flask import abort
from sqlalchemy import select

from .metadata.models import (
    ACCESS_NONE,
    ACCESS_READ,
    ACCESS_WRITE,
    ROLE_PORTAL,
    AppUser,
    Attachment,
    MetaField,
    MetaFieldPermission,
    MetaForm,
    MetaPermission,
    MetaTable,
)


def table_readable(session, user, table):
    """True if ``user`` can read any form of ``table`` (designer = full)."""
    if not user or not user.is_authenticated or getattr(user, "is_portal", False):
        return False
    if getattr(user, "is_designer", False):
        return True
    return any(can_read(form_access(session, user, f.id))
               for f in session.scalars(select(MetaForm).where(MetaForm.table_id == table.id)))


def table_writable(session, user, table):
    if not user or not user.is_authenticated or getattr(user, "is_portal", False):
        return False
    if getattr(user, "is_designer", False):
        return True
    return any(can_write(form_access(session, user, f.id))
               for f in session.scalars(select(MetaForm).where(MetaForm.table_id == table.id)))


def form_access(session, user, form_id):
    """Access level for a user on a form: 'full' (designer) | 'write' | 'read' | 'none'."""
    if not user or not user.is_authenticated or getattr(user, "is_portal", False):
        return "none"
    if user.is_designer:
        return "full"
    perm = session.scalar(
        select(MetaPermission).where(
            MetaPermission.role == user.role, MetaPermission.form_id == form_id
        )
    )
    return perm.access if perm else ACCESS_WRITE   # default: writable (backward-compatible)


def field_access(session, user, field):
    """Per-field access for a user: 'full' (designer) | 'write' | 'read' | 'none'."""
    if not user or not getattr(user, "is_authenticated", False):
        return ACCESS_NONE
    if getattr(user, "is_designer", False):
        return "full"
    perm = session.scalar(select(MetaFieldPermission).where(
        MetaFieldPermission.role == user.role, MetaFieldPermission.field_id == field.id))
    return perm.access if perm else ACCESS_WRITE


def _field_perm_map(session, user):
    return {p.field_id: p.access for p in session.scalars(
        select(MetaFieldPermission).where(MetaFieldPermission.role == user.role))}


def readable_fields(session, user, meta_table):
    """Set of phys-names the user may read (designer = all; default = write)."""
    if not user or not user.is_authenticated or getattr(user, "is_portal", False):
        return set()
    if getattr(user, "is_designer", False):
        return {f.phys_name for f in meta_table.fields}
    perms = _field_perm_map(session, user)
    return {f.phys_name for f in meta_table.fields
            if perms.get(f.id, ACCESS_WRITE) in (ACCESS_WRITE, ACCESS_READ)}


def writable_fields(session, user, meta_table):
    """Set of phys-names the user may write (designer = all; default = write)."""
    if not user or not user.is_authenticated or getattr(user, "is_portal", False):
        return set()
    if getattr(user, "is_designer", False):
        return {f.phys_name for f in meta_table.fields}
    perms = _field_perm_map(session, user)
    return {f.phys_name for f in meta_table.fields if perms.get(f.id, ACCESS_WRITE) == ACCESS_WRITE}


def can_read(access):
    return access in ("full", ACCESS_WRITE, ACCESS_READ)


def can_write(access):
    return access in ("full", ACCESS_WRITE)



def portal_members(session, user):
    from .companies import subtree_ids
    if not user.company_id:
        return {user.id}
    ids = set(session.scalars(select(AppUser.id).where(
        AppUser.company_id.in_(subtree_ids(session, user.company_id)),
        AppUser.role == ROLE_PORTAL)))
    return ids | {user.id}


def portal_record(session, user, table, pk):
    from . import record_service
    from .db import engine_for_table
    row = record_service.get_record(engine_for_table(table), table, pk,
                                    user_id=user.id, is_designer=True)
    if not row or (not user.is_designer and row.get("created_by") not in portal_members(session, user)):
        abort(404)
    return row


def attachment(session, user, att_id, *, portal=False):
    from . import record_service
    from .db import engine_for_table
    if not user.is_authenticated or not user.is_active:
        abort(401)
    att = session.get(Attachment, att_id)
    field = session.get(MetaField, att.field_id) if att else None
    table = session.get(MetaTable, field.table_id) if field else None
    if not table:
        abort(404)
    if portal:
        if not (user.is_portal or user.is_designer):
            abort(403)
        catalog = session.scalar(select(MetaForm.id).where(
            MetaForm.table_id == table.id, MetaForm.in_catalog.is_(True),
            MetaForm.purpose != "view").limit(1))
        if not catalog or not (table.track_audit or table.row_owned):
            abort(404)
        portal_record(session, user, table, att.row_pk)
    else:
        if user.is_portal or not table_readable(session, user, table):
            abort(403)
        if not record_service.get_record(engine_for_table(table), table, att.row_pk,
                                        user_id=user.id, is_designer=user.is_designer,
                                        allow_deleted=True):
            abort(404)
    if not can_read(field_access(session, user, field)):
        abort(403)
    return att


def row_readable(table, row, *, user_id, is_designer, allow_deleted=False, company_scope=None):
    if not row:
        return False
    if table.soft_delete and not allow_deleted and row.get("deleted_at") is not None:
        return False
    if table.row_owned and not is_designer and row.get("created_by") != user_id:
        return False
    return company_scope is None or row.get(company_scope[0]) in company_scope[1]
