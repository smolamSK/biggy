"""Status-workflow rules: allowed transitions between enum states + role gating.

A :class:`~app.metadata.models.Workflow` is attached to an enum field; its
``transitions`` JSON defines which ``from→to`` moves are allowed and, optionally,
which roles may perform each. Enforced on every record update (UI + API) via
:func:`check`. Designers bypass the per-transition *role* gate but not the graph
itself (an illegal state change is a data-integrity error for everyone).
"""
import json

from sqlalchemy import select

from .metadata.models import MetaField, Workflow


class WorkflowError(ValueError):
    """An update that violates the allowed-transition graph or its role gate."""


def for_table(session, table_id):
    """Return ``{field_id: Workflow}`` for the table's workflows (usually 0–1)."""
    rows = session.scalars(select(Workflow).where(Workflow.table_id == table_id)).all()
    return {wf.field_id: wf for wf in rows}


def transitions(wf):
    try:
        data = json.loads(wf.transitions or "[]")
    except (ValueError, TypeError):
        return []
    return [t for t in data if isinstance(t, dict) and t.get("from") and t.get("to")]


def layout(wf):
    try:
        return json.loads(wf.layout or "{}")
    except (ValueError, TypeError):
        return {}


def _match(wf, frm, to):
    """The transition dict for ``frm→to``, or None."""
    for t in transitions(wf):
        if t["from"] == frm and t["to"] == to:
            return t
    return None


def allowed_choices(wf, current, user):
    """States ``current`` may move to (role-permitted), plus ``current`` itself."""
    out = [current] if current is not None else []
    for t in transitions(wf):
        if t["from"] == current and _role_ok(t, user) and t["to"] not in out:
            out.append(t["to"])
    return out


def _role_ok(transition, user):
    roles = transition.get("roles") or []
    if not roles:
        return True
    if getattr(user, "is_designer", False):
        return True
    return getattr(user, "role", None) in roles


def check(session, meta_table, old_row, new_values, user):
    """Raise :class:`WorkflowError` if any status change breaks its workflow."""
    wfs = for_table(session, meta_table.id)
    if not wfs:
        return
    field_by_id = {f.id: f for f in meta_table.fields}
    for field_id, wf in wfs.items():
        field = field_by_id.get(field_id) or session.get(MetaField, field_id)
        if not field or field.phys_name not in new_values:
            continue
        old = (old_row or {}).get(field.phys_name)
        new = new_values[field.phys_name]
        if new == old or new in (None, ""):
            continue
        t = _match(wf, old, new)
        if t is None:
            raise WorkflowError(
                f"{field.label}: “{old}” → “{new}” is not an allowed transition.")
        if not _role_ok(t, user):
            raise WorkflowError(
                f"{field.label}: your role may not perform “{old}” → “{new}”.")
