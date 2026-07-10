"""Per-column filter operators for User-mode list views.

The operator registry is the single source of truth: it is consumed by
:func:`build_clause` (to build SQLAlchemy WHERE clauses) and serialised to the
browser so the add-condition builder can render type-appropriate controls.
"""
from sqlalchemy import and_, or_, select

from .metadata.field_types import RELATION_TYPE

# operators whose value input is ignored
NO_VALUE_OPS = {"empty", "not_empty", "is_true", "is_false"}

# each entry: (key, label, needs_value)
_TEXT = [
    ("contains", "contains", True),
    ("not_contains", "does not contain", True),
    ("starts_with", "starts with", True),
    ("ends_with", "ends with", True),
    ("eq", "equals", True),
    ("ne", "not equals", True),
    ("empty", "is empty", False),
    ("not_empty", "is not empty", False),
]
_NUMBER = [
    ("eq", "equals", True),
    ("ne", "not equals", True),
    ("gt", "greater than", True),
    ("gte", "greater or equal", True),
    ("lt", "less than", True),
    ("lte", "less or equal", True),
    ("empty", "is empty", False),
    ("not_empty", "is not empty", False),
]
_DATE = [
    ("eq", "on", True),
    ("gt", "after", True),
    ("lt", "before", True),
    ("gte", "on or after", True),
    ("lte", "on or before", True),
    ("empty", "is empty", False),
    ("not_empty", "is not empty", False),
]
_ENUM = [
    ("eq", "is", True),
    ("ne", "is not", True),
    ("empty", "is empty", False),
    ("not_empty", "is not empty", False),
]
_BOOL = [
    ("is_true", "is yes", False),
    ("is_false", "is no", False),
    ("empty", "is empty", False),
]
_RELATION = [
    ("eq", "is", True),
    ("ne", "is not", True),
    ("empty", "is empty", False),
    ("not_empty", "is not empty", False),
]

OPS_BY_KIND = {
    "text": _TEXT,
    "number": _NUMBER,
    "date": _DATE,
    "enum": _ENUM,
    "boolean": _BOOL,
    "relation": _RELATION,
    "user": _RELATION,     # is / is not / empty — with user choices (incl. "me")
    "company": _RELATION,  # is / is not / empty — with company choices
}

_KIND_BY_TYPE = {
    "string": "text", "text": "text",
    "email": "text", "url": "text", "phone": "text", "json": "text",
    "autonumber": "text", "tags": "text",
    "integer": "number", "bigint": "number", "decimal": "number", "float": "number",
    "currency": "number", "percent": "number",
    "date": "date", "datetime": "date", "time": "date",
    "enum": "enum",
    "boolean": "boolean",
    "user": "user",
    "company": "company",
}


_KIND_BY_RESULT = {"number": "number", "date": "date", "datetime": "date",
                   "boolean": "boolean"}


def filter_kind(meta_field):
    """Return the filter kind for a :class:`MetaField`."""
    if meta_field.data_type == RELATION_TYPE:
        return "relation"
    if meta_field.data_type == "formula":
        return _KIND_BY_RESULT.get(getattr(meta_field, "result_type", None), "text")
    return _KIND_BY_TYPE.get(meta_field.data_type, "text")


def valid_op(kind, op):
    return any(op == key for key, _, _ in OPS_BY_KIND.get(kind, ()))


def build_meta(session, engine, columns, user=None):
    """Per-column filter UI metadata for list/report condition builders.

    ``columns`` are builder ``FormItem``s (kind ``field``/``relation_m1``) with
    ``.meta``/``.column``/``.label``. Returns
    ``(filter_meta, filter_order, label_maps, m1_targets)`` — the JSON the browser
    uses to render the add-condition controls, plus M:1 label/target maps.
    Relation choices are company-scoped for ``user`` like form pickers are.
    """
    import json

    from . import data_service
    from .forms.builder import _company_where, m1_target_and_columns

    filter_meta, filter_order, label_maps, m1_targets = {}, [], {}, {}
    for it in columns:
        kind = filter_kind(it.meta)
        choices = None
        if it.kind == "relation_m1":
            target, disp_cols = m1_target_and_columns(session, it.meta)
            opts = data_service.load_options(engine, target.phys_name, disp_cols,
                                             where_in=_company_where(session, user,
                                                                     target))
            label_maps[it.column] = dict(opts)
            m1_targets[it.column] = target.id
            choices = [[i, lbl] for i, lbl in opts]
        elif it.meta.data_type == "enum":
            choices = [[o, o] for o in json.loads(it.meta.enum_options or "[]")]
        elif it.meta.data_type == "user":
            from .metadata.models import AppUser
            choices = [["me", "Me"]] + [[u.id, u.username] for u in session.scalars(
                select(AppUser).order_by(AppUser.username))]
        elif it.meta.data_type == "company":
            from .metadata.models import Company
            choices = [[c.id, c.name] for c in session.scalars(
                select(Company).order_by(Company.name))]
        filter_meta[it.column] = {
            "label": it.label, "kind": kind, "data_type": it.meta.data_type,
            "ops": OPS_BY_KIND[kind], "choices": choices,
        }
        filter_order.append(it.column)
    return filter_meta, filter_order, label_maps, m1_targets


def build_clause(column, op, value, *, is_text=False):
    """Return a SQLAlchemy boolean expression for one condition (or None)."""
    if op == "contains":
        return column.contains(value, autoescape=True)
    if op == "not_contains":
        return ~column.contains(value, autoescape=True)
    if op == "starts_with":
        return column.startswith(value, autoescape=True)
    if op == "ends_with":
        return column.endswith(value, autoescape=True)
    if op == "eq":
        return column == value
    if op == "ne":
        return column != value
    if op == "in":
        # internal-only (not in OPS_BY_KIND): e.g. the portal's org-wide scope
        return column.in_(list(value or []))
    if op == "gt":
        return column > value
    if op == "gte":
        return column >= value
    if op == "lt":
        return column < value
    if op == "lte":
        return column <= value
    if op == "is_true":
        return column == True  # noqa: E712 - SQL boolean comparison
    if op == "is_false":
        return column == False  # noqa: E712
    if op == "empty":
        return or_(column.is_(None), column == "") if is_text else column.is_(None)
    if op == "not_empty":
        return and_(column.isnot(None), column != "") if is_text else column.isnot(None)
    return None
