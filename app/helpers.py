"""Shared helpers: access-control decorators, bootstrap detection, menu tree."""
from functools import wraps

from flask import abort, current_app, url_for
from flask_login import current_user
from sqlalchemy import inspect, select

from .db import get_engine
from .extensions import login_manager
from .metadata.models import (
    ACCESS_NONE,
    ACCESS_READ,
    ACCESS_WRITE,
    ROLE_DESIGNER,
    ROLE_USER,
    AppUser,
    MetaFieldPermission,
    MetaForm,
    MetaMenu,
    MetaPermission,
    Role,
)


@login_manager.user_loader
def load_user(user_id):
    from .db import SessionLocal
    return SessionLocal().get(AppUser, int(user_id))


@login_manager.request_loader
def load_user_from_token(request):
    """Authenticate an API request from ``Authorization: Bearer <token>``.

    Only consulted when there is no session user, so browser auth is untouched.
    Returns the token's active user (acting AS them) or None.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    raw = auth[7:].strip()
    if not raw:
        return None
    from datetime import datetime, timezone

    from .api.tokens import hash_token
    from .db import SessionLocal
    from .metadata.models import ApiToken

    session = SessionLocal()
    tok = session.scalar(select(ApiToken).where(
        ApiToken.token_hash == hash_token(raw), ApiToken.revoked.is_(False)))
    if not tok:
        return None
    user = session.get(AppUser, tok.user_id)
    if not user or not user.is_active:
        return None
    try:  # best-effort "last used" stamp
        tok.last_used_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.commit()
    except Exception:  # noqa: BLE001
        session.rollback()
    return user


def table_readable(session, user, table):
    """True if ``user`` can read any form of ``table`` (designer = full)."""
    if getattr(user, "is_designer", False):
        return True
    return any(can_read(form_access(session, user, f.id))
               for f in session.scalars(select(MetaForm).where(MetaForm.table_id == table.id)))


def table_writable(session, user, table):
    if getattr(user, "is_designer", False):
        return True
    return any(can_write(form_access(session, user, f.id))
               for f in session.scalars(select(MetaForm).where(MetaForm.table_id == table.id)))


def designer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return login_manager.unauthorized()
        if not current_user.is_designer:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def is_bootstrapped():
    """True once the metadata tables exist and at least one designer account exists.

    The result is cached on the app once positive (bootstrap only happens once).
    """
    if current_app.config.get("_BOOTSTRAPPED"):
        return True
    try:
        engine = get_engine()
        if not inspect(engine).has_table(AppUser.__tablename__):
            return False
        from .db import SessionLocal
        exists = SessionLocal().scalar(
            select(AppUser.id).where(AppUser.role == ROLE_DESIGNER).limit(1)
        )
    except Exception:  # noqa: BLE001 - DB may be unreachable; treat as not ready
        return False
    if exists:
        current_app.config["_BOOTSTRAPPED"] = True
        return True
    return False


def current_user_id():
    return current_user.id if current_user.is_authenticated else None


def ensure_roles(session):
    """Idempotently seed the built-in roles (designer + user)."""
    existing = {r.name for r in session.scalars(select(Role))}
    added = False
    for name, label in ((ROLE_DESIGNER, "Designer"), (ROLE_USER, "User")):
        if name not in existing:
            session.add(Role(name=name, label=label, builtin=True))
            added = True
    if added:
        session.commit()


def form_access(session, user, form_id):
    """Access level for a user on a form: 'full' (designer) | 'write' | 'read' | 'none'."""
    if not user or not user.is_authenticated:
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
    if getattr(user, "is_designer", False):
        return {f.phys_name for f in meta_table.fields}
    perms = _field_perm_map(session, user)
    return {f.phys_name for f in meta_table.fields
            if perms.get(f.id, ACCESS_WRITE) in (ACCESS_WRITE, ACCESS_READ)}


def writable_fields(session, user, meta_table):
    """Set of phys-names the user may write (designer = all; default = write)."""
    if getattr(user, "is_designer", False):
        return {f.phys_name for f in meta_table.fields}
    perms = _field_perm_map(session, user)
    return {f.phys_name for f in meta_table.fields if perms.get(f.id, ACCESS_WRITE) == ACCESS_WRITE}


def can_read(access):
    return access in ("full", ACCESS_WRITE, ACCESS_READ)


def can_write(access):
    return access in ("full", ACCESS_WRITE)


def table_view_form(session, table_id):
    """The table's read-only 'view' form, or None."""
    return session.scalar(
        select(MetaForm).where(MetaForm.table_id == table_id, MetaForm.purpose == "view")
        .order_by(MetaForm.id).limit(1))


def can_view(session, user, table_id):
    """A record of this table is viewable if it has a view form the user can read."""
    vf = table_view_form(session, table_id)
    return bool(vf) and can_read(form_access(session, user, vf.id))


def menu_visible(session, user, item):
    """Whether a menu item should appear for the user (read access to its target)."""
    if item.kind == "group":
        return any(menu_visible(session, user, c) for c in item.children)
    if item.kind == "form" and item.target_form_id:
        return can_read(form_access(session, user, item.target_form_id))
    if item.kind == "list" and item.target_table_id:
        form = session.scalar(
            select(MetaForm).where(MetaForm.table_id == item.target_table_id)
            .order_by(MetaForm.id).limit(1)
        )
        return bool(form) and can_read(form_access(session, user, form.id))
    if item.kind == "dashboard" and item.target_dashboard_id:
        from . import dashboards
        from .metadata.models import Dashboard
        dash = session.get(Dashboard, item.target_dashboard_id)
        return bool(dash) and dashboards.visible(session, user, dash)
    return False


def menu_tree():
    """Return the ordered top-level menu items (with children) for User mode."""
    from .db import SessionLocal
    session = SessionLocal()
    roots = session.scalars(
        select(MetaMenu).where(MetaMenu.parent_id.is_(None)).order_by(MetaMenu.position)
    ).all()
    return roots


def menu_url(item):
    """Resolve a menu item to a User-mode URL ('#' if it targets nothing)."""
    from .db import SessionLocal
    from .metadata.models import MetaForm

    if item.kind == "form" and item.target_form_id:
        return url_for("user.form_list", form_id=item.target_form_id)
    if item.kind == "list" and item.target_table_id:
        session = SessionLocal()
        form = session.scalar(
            select(MetaForm).where(MetaForm.table_id == item.target_table_id)
            .order_by(MetaForm.id).limit(1)
        )
        if form:
            return url_for("user.form_list", form_id=form.id)
    if item.kind == "dashboard" and item.target_dashboard_id:
        return url_for("user.dashboard_view", dash_id=item.target_dashboard_id)
    return "#"
