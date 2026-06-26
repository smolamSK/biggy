"""Safe formula evaluation for computed fields.

A *formula* is a small expression over a record's own columns and — via the
``lookup()`` / ``rollup()`` functions — related tables. Expressions are parsed and
walked with :mod:`ast` (never ``eval``): only a fixed whitelist of node types and
functions is permitted, so a formula can never reach Python internals.

The evaluator (:func:`evaluate`, :func:`validate`, :func:`coerce_result`) is pure
and DB-free; cross-table access is delegated to an injected ``resolver`` (a stub
in unit tests). The DB-aware :class:`Resolver` and the ``compute_*`` /
``recompute_*`` helpers wire it to real records via :mod:`app.data_service`.
"""
import ast
import operator
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import or_, select

from .metadata.field_types import RELATION_TYPE

FORMULA_TYPE = "formula"
RESULT_TYPES = ("number", "text", "boolean", "date", "datetime")


class FormulaError(ValueError):
    """Raised for a structurally invalid / disallowed formula."""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _is_temporal(x):
    return isinstance(x, (date, datetime))


def _is_number(x):
    return isinstance(x, (int, float, Decimal)) and not isinstance(x, bool)


def _as_text(x):
    if x is None:
        return ""
    if isinstance(x, datetime):
        return x.isoformat(sep=" ")
    if isinstance(x, date):
        return x.isoformat()
    return str(x)


def _dec(x):
    if isinstance(x, bool):
        return Decimal(1) if x else Decimal(0)
    return Decimal(str(x))


def _to_int(x):
    return int(_dec(x))


_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg, ast.Not: operator.not_}
_CMPOPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
           ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge}


def _date_arith(op_type, a, b):
    if op_type is ast.Sub and _is_temporal(a) and _is_temporal(b):
        d = (a - b) if isinstance(a, type(b)) else (
            (a if isinstance(a, datetime) else datetime(a.year, a.month, a.day))
            - (b if isinstance(b, datetime) else datetime(b.year, b.month, b.day)))
        return d.days
    if op_type is ast.Sub and _is_temporal(a):
        return a - timedelta(days=_to_int(b))
    if op_type is ast.Add and _is_temporal(a):
        return a + timedelta(days=_to_int(b))
    if op_type is ast.Add and _is_temporal(b):
        return b + timedelta(days=_to_int(a))
    return None


def _binop(op_type, a, b):
    if op_type is ast.BitAnd:                       # '&' = string concat
        return _as_text(a) + _as_text(b)
    if a is None or b is None:
        return None
    if op_type in (ast.Add, ast.Sub) and (_is_temporal(a) or _is_temporal(b)):
        return _date_arith(op_type, a, b)
    fn = _BINOPS.get(op_type)
    if fn is None:
        raise FormulaError("operator not allowed")
    try:
        return fn(_dec(a), _dec(b))
    except (InvalidOperation, ZeroDivisionError, ArithmeticError, ValueError, TypeError):
        try:
            return fn(float(a), float(b))
        except Exception:  # noqa: BLE001 - best effort
            return None


def _coalesce(*args):
    for a in args:
        if a is not None and a != "":
            return a
    return None


def _safe(fn):
    def wrapped(*a):
        try:
            return fn(*a)
        except Exception:  # noqa: BLE001 - builtins are best-effort
            return None
    return wrapped


_FUNCS = {
    "today": lambda: date.today(),
    "now": lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    "round": _safe(lambda x, n=0: round(_dec(x), int(n)) if x is not None else None),
    "abs": _safe(lambda x: abs(_dec(x)) if x is not None else None),
    "min": _safe(lambda *a: min([v for v in a if v is not None], default=None)),
    "max": _safe(lambda *a: max([v for v in a if v is not None], default=None)),
    "len": _safe(lambda x: len(x) if x is not None else 0),
    "upper": _safe(lambda s: _as_text(s).upper() if s is not None else None),
    "lower": _safe(lambda s: _as_text(s).lower() if s is not None else None),
    "trim": _safe(lambda s: _as_text(s).strip() if s is not None else None),
    "str": _safe(lambda x: _as_text(x)),
    "int": _safe(lambda x: int(_dec(x)) if x not in (None, "") else None),
    "float": _safe(lambda x: float(_dec(x)) if x not in (None, "") else None),
    "coalesce": _coalesce,
    "contains": _safe(lambda s, sub: _as_text(sub) in _as_text(s)),
}
_RELATION_FUNCS = ("lookup", "rollup")


# --------------------------------------------------------------------------- #
# Evaluation
# --------------------------------------------------------------------------- #
def _eval(node, ctx, resolver):
    if isinstance(node, ast.Expression):
        return _eval(node.body, ctx, resolver)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return ctx.get(node.id)
    if isinstance(node, ast.UnaryOp):
        fn = _UNARYOPS.get(type(node.op))
        if fn is None:
            raise FormulaError("unary operator not allowed")
        v = _eval(node.operand, ctx, resolver)
        if isinstance(node.op, ast.Not):
            return not v
        if v is None:
            return None
        try:
            return fn(_dec(v))
        except Exception:  # noqa: BLE001
            return None
    if isinstance(node, ast.BinOp):
        return _binop(type(node.op),
                      _eval(node.left, ctx, resolver), _eval(node.right, ctx, resolver))
    if isinstance(node, ast.BoolOp):
        vals = [_eval(v, ctx, resolver) for v in node.values]
        if isinstance(node.op, ast.And):
            for v in vals:
                if not v:
                    return v
            return vals[-1]
        for v in vals:                              # Or
            if v:
                return v
        return vals[-1]
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx, resolver)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, ctx, resolver)
            fn = _CMPOPS.get(type(op))
            if fn is None:
                raise FormulaError("comparison not allowed")
            try:
                ok = fn(left, right)
            except TypeError:
                ok = False
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return (_eval(node.body, ctx, resolver) if _eval(node.test, ctx, resolver)
                else _eval(node.orelse, ctx, resolver))
    if isinstance(node, ast.Call):
        return _eval_call(node, ctx, resolver)
    raise FormulaError(f"unsupported expression: {type(node).__name__}")


def _eval_call(node, ctx, resolver):
    if not isinstance(node.func, ast.Name) or node.keywords:
        raise FormulaError("only simple named function calls are allowed")
    name = node.func.id
    args = [_eval(a, ctx, resolver) for a in node.args]
    if name in _RELATION_FUNCS:
        if resolver is None:
            return None
        return getattr(resolver, name)(*args)
    fn = _FUNCS.get(name)
    if fn is None:
        raise FormulaError(f"unknown function: {name}")
    return fn(*args)


def evaluate(expr, context, resolver=None):
    """Evaluate ``expr`` against ``context`` (column→value). Returns the raw value."""
    if not expr or not str(expr).strip():
        return None
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"syntax error: {exc.msg}")
    return _eval(tree, context or {}, resolver)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
_ALLOWED_NODES = (
    ast.Expression, ast.Constant, ast.Name, ast.Load,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


def _check_ref(call, allowed, kind):
    if not call.args or not isinstance(call.args[0], ast.Constant) \
            or not isinstance(call.args[0].value, str):
        return f"{kind}(): the first argument must be a name in quotes."
    if allowed is not None and call.args[0].value not in allowed:
        what = "relation" if kind == "rollup" else "link field"
        return f"{kind}(): unknown {what} '{call.args[0].value}'."
    return None


def validate(expr, columns, lookup_fields=None, rollup_rels=None):
    """Return an error message, or ``None`` if the formula is safe and resolvable."""
    if not expr or not str(expr).strip():
        return "Formula is empty."
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError as exc:
        return f"Syntax error: {exc.msg}"
    columns = set(columns or [])
    funcs = set(_FUNCS) | set(_RELATION_FUNCS)
    func_names = set()
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            return f"Not allowed here: {type(node).__name__}."
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in funcs:
                return "Unknown or disallowed function."
            if node.keywords:
                return "Keyword arguments are not allowed."
            func_names.add(node.func.id)
            if node.func.id == "lookup":
                err = _check_ref(node, lookup_fields, "lookup")
                if err:
                    return err
            elif node.func.id == "rollup":
                err = _check_ref(node, rollup_rels, "rollup")
                if err:
                    return err
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id not in func_names \
                and node.id not in columns:
            return f"Unknown field: {node.id}."
    return None


# --------------------------------------------------------------------------- #
# Result coercion
# --------------------------------------------------------------------------- #
def coerce_result(value, result_type):
    """Coerce a computed value to the field's stored type; ``None`` on mismatch."""
    if value is None:
        return None
    try:
        if result_type == "text":
            return _as_text(value)
        if result_type == "boolean":
            return bool(value)
        if result_type == "date":
            if isinstance(value, datetime):
                return value.date()
            return value if isinstance(value, date) else None
        if result_type == "datetime":
            if isinstance(value, datetime):
                return value
            return datetime(value.year, value.month, value.day) if isinstance(value, date) else None
        # number (default)
        if isinstance(value, bool):
            return Decimal(1) if value else Decimal(0)
        if _is_number(value):
            return Decimal(str(value))
        if _is_temporal(value):
            return None
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# DB-aware resolver + recompute helpers
# --------------------------------------------------------------------------- #
class Resolver:
    """Backs ``lookup``/``rollup`` for one record against the live database."""

    def __init__(self, session, engine, meta_table, row):
        self.session, self.engine, self.meta_table = session, engine, meta_table
        self.row = row or {}

    def lookup(self, rel_field, field):
        from . import data_service
        from .metadata.models import MetaTable
        if not rel_field or not field:
            return None
        mf = next((f for f in self.meta_table.fields
                   if f.phys_name == rel_field and f.data_type == RELATION_TYPE), None)
        if not mf or not mf.related_table_id:
            return None
        fk = self.row.get(rel_field)
        if fk in (None, ""):
            return None
        parent = self.session.get(MetaTable, mf.related_table_id)
        prow = data_service.get_row(self.engine, parent.phys_name, fk) if parent else None
        return prow.get(field) if prow else None

    def rollup(self, rel, field, op="count"):
        from . import data_service
        from .metadata.models import MetaField, MetaRelation, MetaTable
        op = (op or "count").lower()
        zero = 0 if op == "count" else None
        pk = self.row.get(self.meta_table.pk_col)
        if pk in (None, "") or not rel:
            return zero
        relation = self.session.scalar(select(MetaRelation).where(MetaRelation.name == rel))
        if not relation:
            return zero
        # incoming many-to-one: children whose FK points at this record
        if relation.kind == "m1" and relation.to_table_id == self.meta_table.id:
            child = self.session.get(MetaTable, relation.from_table_id)
            fkf = self.session.get(MetaField, relation.from_field_id)
            if not child or not fkf:
                return zero
            return data_service.aggregate_value(
                self.engine, child.phys_name, op, field, where_col=fkf.phys_name, where_val=pk)
        # many-to-many: linked rows via the junction
        if relation.kind == "mn":
            other_id = (relation.to_table_id if relation.from_table_id == self.meta_table.id
                        else relation.from_table_id)
            other = self.session.get(MetaTable, other_id)
            if not other:
                return zero
            this_col = f"{self.meta_table.phys_name}_id"
            other_col = f"{other.phys_name}_id"
            if this_col == other_col:
                other_col = f"{other.phys_name}_id_2"
            ids = data_service.get_links(
                self.engine, relation.junction_phys_name, this_col, pk, other_col)
            if op == "count":
                return len(ids)
            rows = data_service.rows_by_ids(self.engine, other.phys_name, ids) if ids else []
            return _agg_values([r.get(field) for r in rows], op)
        return zero


def _agg_values(vals, op):
    nums = [v for v in vals if _is_number(v)]
    if not nums:
        return None
    if op == "sum":
        return sum(nums)
    if op == "avg":
        return sum(nums) / len(nums)
    if op == "min":
        return min(nums)
    if op == "max":
        return max(nums)
    return None


def _formula_fields(meta_table):
    return [f for f in sorted(meta_table.fields, key=lambda x: x.position)
            if f.data_type == FORMULA_TYPE]


def compute_values(session, engine, meta_table, context, pk):
    """Return ``{col: value}`` for every formula field of ``meta_table``."""
    fields = _formula_fields(meta_table)
    if not fields:
        return {}
    row = dict(context or {})
    if pk is not None:
        row.setdefault(meta_table.pk_col, pk)
    resolver = Resolver(session, engine, meta_table, row)
    out = {}
    for f in fields:
        try:
            value = evaluate(f.formula, row, resolver)
        except FormulaError:
            value = None
        value = coerce_result(value, f.result_type or "number")
        out[f.phys_name] = value
        row[f.phys_name] = value          # a later formula may use an earlier one
    return out


def _recompute_row(session, engine, meta_table, row):
    from . import data_service
    pk = row.get(meta_table.pk_col) if row else None
    if pk is None or not _formula_fields(meta_table):
        return
    vals = compute_values(session, engine, meta_table, row, pk)
    if vals:
        data_service.update_row(engine, meta_table.phys_name, pk, vals)


def recompute_table(session, engine, meta_table):
    """Recompute all formula columns for every row (backfill / after import)."""
    from . import data_service
    if not _formula_fields(meta_table):
        return
    for row in data_service.list_rows_after(engine, meta_table.phys_name, 0, limit=10 ** 9):
        _recompute_row(session, engine, meta_table, row)


def recompute_related(session, engine, meta_table, row):
    """Recompute formula columns of records that may depend on this one.

    ``row`` is the written record's values (the *old* row on delete). Refreshes the
    parents it points at (their rollups) and the children / linked rows that point
    at it (their lookups). Uses :func:`data_service.update_row` directly, so it does
    not re-trigger the ripple.
    """
    from . import data_service
    from .metadata.models import MetaField, MetaRelation, MetaTable
    if not row:
        return
    rid = row.get(meta_table.pk_col)

    # parents this record points at (their rollups include/excluded this row)
    for f in meta_table.fields:
        if f.data_type == RELATION_TYPE and f.related_table_id and row.get(f.phys_name):
            parent = session.get(MetaTable, f.related_table_id)
            if parent and _formula_fields(parent):
                prow = data_service.get_row(engine, parent.phys_name, row[f.phys_name])
                _recompute_row(session, engine, parent, prow)
    if rid is None:
        return

    # children via incoming M:1 (their lookups read this row)
    for rel in session.scalars(select(MetaRelation).where(
            MetaRelation.kind == "m1", MetaRelation.to_table_id == meta_table.id)):
        child = session.get(MetaTable, rel.from_table_id)
        fkf = session.get(MetaField, rel.from_field_id)
        if not child or not fkf or not _formula_fields(child):
            continue
        rows, _t = data_service.list_rows(
            engine, child.phys_name,
            filters=[{"col": fkf.phys_name, "op": "eq", "value": rid}], per_page=10 ** 6)
        for crow in rows:
            _recompute_row(session, engine, child, crow)

    # linked rows via M:N
    for rel in session.scalars(select(MetaRelation).where(
            MetaRelation.kind == "mn",
            or_(MetaRelation.from_table_id == meta_table.id,
                MetaRelation.to_table_id == meta_table.id))):
        other_id = (rel.to_table_id if rel.from_table_id == meta_table.id
                    else rel.from_table_id)
        other = session.get(MetaTable, other_id)
        if not other or not _formula_fields(other):
            continue
        this_col = f"{meta_table.phys_name}_id"
        other_col = f"{other.phys_name}_id"
        if this_col == other_col:
            other_col = f"{other.phys_name}_id_2"
        for oid in data_service.get_links(engine, rel.junction_phys_name, this_col, rid, other_col):
            _recompute_row(session, engine, other, data_service.get_row(engine, other.phys_name, oid))
