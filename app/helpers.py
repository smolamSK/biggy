"""Shared helpers: access-control decorators, bootstrap detection, menu tree."""
from functools import wraps

from flask import abort, current_app, url_for
from flask_login import current_user
from sqlalchemy import inspect, select

from .access import (  # noqa: F401 - public compatibility exports
    _field_perm_map,
    can_read,
    can_write,
    field_access,
    form_access,
    readable_fields,
    table_readable,
    table_writable,
    writable_fields,
)
from .db import get_engine
from .extensions import login_manager
from .metadata.models import (
    ROLE_DESIGNER,
    ROLE_USER,
    AppUser,
    MetaForm,
    MetaMenu,
    Role,
)


@login_manager.user_loader
def load_user(user_id):
    from .db import SessionLocal
    try:
        user = SessionLocal().get(AppUser, int(user_id))
    except (TypeError, ValueError):
        return None
    return user if user and user.is_active else None


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


# Status-chip hues (must match the .chip.c-* classes in static/app.css and
# CHIP_HUES in static/inline.js).
CHIP_HUES = ("green", "amber", "red", "blue", "violet", "teal", "gray")

# Canonical names of the built-in SVG icons (templates/_icons.html). The menu
# editor offers these; the icon() macro silently skips names it doesn't know,
# so removing an icon can never break rendering.
ICON_NAMES = (
    "bell", "calendar", "chart", "check", "check-circle", "columns", "copy",
    "download", "eye", "filter", "help", "home", "inbox", "kanban", "list",
    "menu", "pencil", "pin", "plus", "search", "trash", "user", "zap",
)


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
