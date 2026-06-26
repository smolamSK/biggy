"""Render dashboards: chart, KPI-number, list and text/markdown tiles.

A :class:`~app.metadata.models.Dashboard` is a grid of
:class:`~app.metadata.models.DashboardWidget` tiles. **Shared** dashboards
(``owner_user_id`` NULL) are designer-built and gated by who can read the
underlying tables; **personal** ones belong to a single user. Reuses the reporting
+ chart pipeline (:mod:`app.reporting` + ``static/charts.js``), ``record_service``
for list tiles, and ``markdown`` for text tiles.
"""
from urllib.parse import parse_qsl

from markupsafe import Markup, escape
from sqlalchemy import select
from werkzeug.datastructures import MultiDict

from . import helpers, record_service, reporting
from .db import engine_for_table
from .metadata.field_types import RELATION_TYPE
from .metadata.models import MetaForm, MetaTable

try:  # optional dependency (same as app.help)
    import markdown as _markdown
except ImportError:  # pragma: no cover
    _markdown = None


def _md(text):
    """Render trusted (designer/owner-authored) markdown to safe HTML."""
    if not text:
        return ""
    if _markdown is None:
        return Markup(f"<pre>{escape(text)}</pre>")
    return Markup(_markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"]))


def visible(session, user, dash):
    """Whether ``user`` may see ``dash`` (personal ⇒ owner; shared ⇒ any readable table)."""
    if dash.owner_user_id is not None:
        return bool(getattr(user, "is_authenticated", False)) and dash.owner_user_id == user.id
    table_ids = {w.table_id for w in dash.widgets if w.table_id}
    if not table_ids:
        return True
    for tid in table_ids:
        t = session.get(MetaTable, tid)
        if t and helpers.table_readable(session, user, t):
            return True
    return False


def _list_columns(table):
    return [f for f in table.fields
            if f.data_type not in (RELATION_TYPE, "file", "image")][:4]


def render(session, user, dash):
    """Return a list of rendered tile dicts, skipping widgets the user can't read."""
    uid = user.id if getattr(user, "is_authenticated", False) else None
    is_designer = bool(getattr(user, "is_designer", False))
    tiles = []
    for w in dash.widgets:
        table = session.get(MetaTable, w.table_id) if w.table_id else None
        if table and not helpers.table_readable(session, user, table):
            continue
        tile = {"w": w, "title": w.title, "kind": w.kind,
                "width": min(2, max(1, w.width or 1))}
        if w.kind == "text":
            tile["html"] = _md(w.content)
        elif w.kind in ("chart", "number") and table:
            pairs = [(k, v) for k, v in parse_qsl(w.query or "")
                     if not (w.kind == "number" and k == "group")]   # number ⇒ ungrouped total
            scope = record_service._scope_filters(
                table, user_id=uid, is_designer=is_designer, include_deleted=False)
            ctx = reporting.build(session, engine_for_table(table), table,
                                  MultiDict(pairs), base_filters=scope, user=user)
            if w.kind == "chart":
                tile["chart_type"] = w.chart_type or "bar"
                tile["chart_data"] = ctx["chart_data"]
            else:
                res = ctx["result"]
                rows, titles = res.get("rows") or [], res.get("titles") or []
                tile["metric"] = titles[0] if titles else "Value"
                tile["value"] = reporting._num(rows[0][0] if rows and rows[0] else 0)
                tile["target"] = (w.content or "").strip() or None
        elif w.kind == "list" and table:
            rows, _total = record_service.list_records(
                engine_for_table(table), table, user_id=uid, is_designer=is_designer,
                per_page=max(1, w.limit or 5))
            form = session.scalar(select(MetaForm).where(MetaForm.table_id == table.id)
                                  .order_by(MetaForm.id).limit(1))
            tile.update(table=table, columns=_list_columns(table), rows=rows,
                        form_id=form.id if form else None)
        else:
            continue
        tiles.append(tile)
    return tiles
