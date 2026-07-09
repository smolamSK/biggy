"""Enable-able ITIL process modules: Incidents, Requests, Problems + Known
errors, Change management.

Each module is a self-contained schema fragment (tables, forms, workflow, SLA,
catalog cards) authored with the same :class:`~app.examples.ModelBuilder` the
demos use, imported **additively** next to whatever model already exists — at
setup time or any point later. After enabling, :func:`wire_cross_links` adds
the classic ITIL relations wherever both ends exist (incident → problem,
change → problem, incident/change → a ``ci`` table, …), so modules enabled in
any order — or combined with your own CMDB tables — end up linked.
"""
import logging

from sqlalchemy import func, select

from . import schema_io
from .db import engine_for_table, get_engine
from .examples import (
    ModelBuilder,
    add_changes,
    add_incidents,
    add_problems,
    add_requests,
)
from .metadata import schema_service
from .metadata.models import (
    MetaField,
    MetaForm,
    MetaFormField,
    MetaMenu,
    MetaRelation,
    MetaTable,
)

_logger = logging.getLogger(__name__)

MODULES = {
    "incidents": {
        "title": "Incident management",
        "description": "Unplanned interruptions: priorities, lifecycle workflow, "
                       "a 4-hour resolution SLA and a portal catalog card.",
        "tables": ("incident",), "build": add_incidents,
        "menus": [("Incidents", "incident_form")],
    },
    "requests": {
        "title": "Request fulfilment",
        "description": "User requests (access, hardware, …) with urgency, "
                       "workflow, a fulfilment SLA and a portal catalog card.",
        "tables": ("request",), "build": add_requests,
        "menus": [("Requests", "request_form")],
    },
    "problems": {
        "title": "Problem management + known errors",
        "description": "Root-cause records behind recurring incidents, plus a "
                       "known-error database with documented workarounds.",
        "tables": ("problem", "known_error"), "build": add_problems,
        "menus": [("Problems", "problem_form"), ("Known errors", "known_error_form")],
    },
    "changes": {
        "title": "Change management",
        "description": "Typed and risk-rated changes with implementation/backout "
                       "plans, a lifecycle workflow and CAB approval "
                       "(change_manager role) on assessing → approved.",
        "tables": ("change",), "build": add_changes,
        "menus": [("Changes", "change_form")],
    },
}

# ITIL relations added automatically once both ends exist (in any order):
# (from table, to table, FK column, relation label)
CROSS_LINKS = [
    ("incident", "problem", "problem_id", "Problem"),
    ("incident", "change", "caused_by_change_id", "Caused by change"),
    ("change", "problem", "problem_id", "Fixes problem"),
    ("incident", "ci", "ci_id", "Configuration item"),
    ("change", "ci", "ci_id", "Configuration item"),
]


def status(session):
    """``{module key: enabled?}`` — a module is enabled when its tables exist."""
    phys = {t.phys_name for t in session.scalars(select(MetaTable))}
    return {key: all(t in phys for t in mod["tables"])
            for key, mod in MODULES.items()}


def enable(session, key):
    """Add one module next to the existing model. Returns True when added,
    False when it was already enabled. Raises SchemaError on collisions."""
    mod = MODULES[key]
    phys = {t.phys_name for t in session.scalars(select(MetaTable))}
    if all(t in phys for t in mod["tables"]):
        return False
    b = ModelBuilder()
    mod["build"](b)
    schema_io.import_schema(session, get_engine(), b.schema(), additive=True)
    _wire_menus(session, mod)
    wire_cross_links(session)
    session.commit()
    return True


def _wire_menus(session, mod):
    """Put the module's forms under a shared 'ITSM' sidebar group."""
    group = session.scalar(select(MetaMenu).where(
        MetaMenu.kind == "group", MetaMenu.label == "ITSM",
        MetaMenu.parent_id.is_(None)))
    if group is None:
        group = MetaMenu(label="ITSM", kind="group",
                         position=_next_menu_pos(session))
        session.add(group)
        session.flush()
    for label, form_name in mod["menus"]:
        mf = session.scalar(select(MetaForm).where(MetaForm.name == form_name))
        if mf is None:
            continue
        if session.scalar(select(MetaMenu.id).where(
                MetaMenu.parent_id == group.id, MetaMenu.target_form_id == mf.id)):
            continue
        session.add(MetaMenu(label=label, kind="form", parent_id=group.id,
                             target_form_id=mf.id,
                             position=_next_menu_pos(session)))
        session.flush()


def _next_menu_pos(session):
    return (session.scalar(select(func.max(MetaMenu.position))) or 0) + 1


def wire_cross_links(session):
    """Create every :data:`CROSS_LINKS` relation whose tables both exist and
    whose column doesn't yet — and surface it on the source table's forms.
    Idempotent; returns the number of links added."""
    tables = {t.phys_name: t for t in session.scalars(select(MetaTable))}
    added = 0
    for from_phys, to_phys, col, label in CROSS_LINKS:
        ft, tt = tables.get(from_phys), tables.get(to_phys)
        if ft is None or tt is None or not ft.managed:
            continue
        if any(f.phys_name == col for f in ft.fields):
            continue
        mf = MetaField(table_id=ft.id, phys_name=col, label=label,
                       data_type="relation", nullable=True,
                       position=len(ft.fields), related_table_id=tt.id,
                       on_delete="SET NULL")
        session.add(mf)
        session.flush()
        schema_service.add_relation_column(engine_for_table(ft), ft.phys_name,
                                           mf, tt.phys_name)
        session.add(MetaRelation(name=label, kind="m1", from_table_id=ft.id,
                                 to_table_id=tt.id, from_field_id=mf.id,
                                 on_delete="SET NULL"))
        for form in session.scalars(select(MetaForm).where(
                MetaForm.table_id == ft.id)):
            session.add(MetaFormField(form_id=form.id, kind="field",
                                      field_id=mf.id, position=len(form.items)))
        session.flush()
        added += 1
    return added
