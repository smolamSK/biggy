"""Guided group-by / aggregation reports over a user table.

Builds the report from validated metadata (no raw SQL): the heavy lifting is
``data_service.aggregate_rows``; this module turns request args + a table's
fields into group/metric/filter choices and shapes the result for rendering and
CSV. Used by both the User-mode and Designer-mode report routes.
"""
import csv
import io
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

from . import data_service
from . import filters as filt
from .metadata.field_types import FILE_TYPES, RELATION_TYPE

NUMERIC = {"integer", "bigint", "decimal", "float"}
DATELIKE = {"date", "datetime"}
_SCALAR_FUNCS = {"sum", "avg", "min", "max"}


@dataclass
class Col:
    kind: str        # 'field' | 'relation_m1'
    column: str
    label: str
    meta: object


def table_columns(table, allowed=None):
    """Reportable columns (scalar + M:1); file/image and unreadable fields excluded."""
    out = []
    for f in table.fields:
        if f.data_type in FILE_TYPES:
            continue
        if allowed is not None and f.phys_name not in allowed:
            continue
        kind = "relation_m1" if f.data_type == RELATION_TYPE else "field"
        out.append(Col(kind=kind, column=f.phys_name, label=f.label, meta=f))
    return out


def group_choices(columns):
    return [(c.column, c.label) for c in columns]


def metric_choices(columns):
    """Selectable metrics as ``(key, label)`` — Count plus per-column aggregates."""
    out = [("count", "Count")]
    for c in columns:
        dt = c.meta.data_type
        if dt in NUMERIC:
            funcs = ("sum", "avg", "min", "max")
        elif dt in DATELIKE:
            funcs = ("min", "max")
        else:
            continue
        for fn in funcs:
            out.append((f"{fn}:{c.column}", f"{fn.title()} of {c.label}"))
    return out


def _parse_metric(key, by_col):
    if key == "count":
        return {"func": "count", "col": None, "label": "count", "title": "Count"}
    func, _, col = key.partition(":")
    c = by_col.get(col)
    if func not in _SCALAR_FUNCS or not c:
        return None
    dt = c.meta.data_type
    if func in ("sum", "avg") and dt not in NUMERIC:
        return None
    if func in ("min", "max") and dt not in (NUMERIC | DATELIKE):
        return None
    return {"func": func, "col": col, "label": f"{func}_{col}",
            "title": f"{func.title()} of {c.label}"}


def parse(args, columns, filter_meta):
    """Return ``(group_col, metrics, filters, conditions)`` from request args."""
    by_col = {c.column: c for c in columns}
    group = args.get("group") or ""
    group_col = group if group in by_col else None

    metrics = []
    for key in args.getlist("metric"):
        m = _parse_metric(key, by_col)
        if m and m not in metrics:
            metrics.append(m)
    if not metrics:
        metrics = [{"func": "count", "col": None, "label": "count", "title": "Count"}]

    filters, conditions = [], []
    for col, op, val in zip(args.getlist("fcol"), args.getlist("fop"), args.getlist("fval")):
        meta = filter_meta.get(col)
        if not meta or not filt.valid_op(meta["kind"], op):
            continue
        conditions.append({"col": col, "op": op, "val": val})
        if op in filt.NO_VALUE_OPS or val != "":
            filters.append({"col": col, "op": op, "value": val,
                            "is_text": meta["kind"] == "text"})
    return group_col, metrics, filters, conditions


def run(engine, table, columns, group_col, metrics, base_filters, label_maps, *, limit=1000):
    """Execute the aggregation and shape it for display.

    Returns ``{grouped, group_label, titles, rows, totals}``. For a relation
    group the id cells are replaced by the referenced record's label.
    """
    _labels, rows, totals = data_service.aggregate_rows(
        engine, table.phys_name, group_col, metrics, filters=base_filters, limit=limit)
    titles = [m["title"] for m in metrics]
    grouped = group_col is not None

    if grouped:
        lmap = label_maps.get(group_col)
        display = []
        for r in rows:
            cell = r[0]
            if lmap is not None:
                cell = lmap.get(cell, cell)
            if cell in (None, ""):
                cell = "—"
            display.append([cell] + list(r[1:]))
        by_col = {c.column: c for c in columns}
        group_label = by_col[group_col].label if group_col in by_col else group_col
        return {"grouped": True, "group_label": group_label, "titles": titles,
                "rows": display, "totals": (list(totals) if totals else None)}

    # no grouping: a single total row of the metrics
    single = list(totals) if totals else [None] * len(metrics)
    return {"grouped": False, "group_label": None, "titles": titles,
            "rows": [single], "totals": None}


def key_of(metric):
    """The checkbox key for a parsed metric (inverse of _parse_metric)."""
    if metric["func"] == "count" and not metric["col"]:
        return "count"
    return f'{metric["func"]}:{metric["col"]}'


def _num(v):
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def chart_data(result):
    """JSON-able ``{grouped, labels, series:[{name, values}]}`` for charts.js."""
    titles = result["titles"]
    if result["grouped"]:
        labels = ["" if r[0] is None else str(r[0]) for r in result["rows"]]
        series = [{"name": titles[j], "values": [_num(r[j + 1]) for r in result["rows"]]}
                  for j in range(len(titles))]
    else:
        row = result["rows"][0] if result["rows"] else []
        labels = list(titles)
        series = [{"name": "Total", "values": [_num(v) for v in row]}]
    return {"grouped": result["grouped"], "labels": labels, "series": series}


def build(session, engine, table, args, base_filters=None, user=None):
    """Parse args + run the report; return the full template context for a table."""
    allowed = None
    if user is not None and not getattr(user, "is_designer", False):
        from .helpers import readable_fields
        allowed = readable_fields(session, user, table)
    columns = table_columns(table, allowed)
    filter_meta, filter_order, label_maps, _m1 = filt.build_meta(session, engine, columns)
    group_col, metrics, filters, conditions = parse(args, columns, filter_meta)
    result = run(engine, table, columns, group_col, metrics,
                 (base_filters or []) + filters, label_maps)
    query_base = urlencode([(k, v) for k, v in args.items(multi=True)
                            if k not in ("export", "chart")])
    return {
        "table": table,
        "group_options": group_choices(columns),
        "metric_options": metric_choices(columns),
        "group_col": group_col or "",
        "selected_metrics": {key_of(m) for m in metrics},
        "filter_meta": filter_meta, "filter_order": filter_order, "conditions": conditions,
        "result": result,
        "chart": args.get("chart") or "bar",
        "chart_data": chart_data(result),
        "query_base": query_base,
    }


def to_csv(result):
    buf = io.StringIO()
    w = csv.writer(buf)
    header = ([result["group_label"]] if result["grouped"] else []) + result["titles"]
    w.writerow(header)
    for r in result["rows"]:
        w.writerow(["" if v is None else v for v in r])
    if result["grouped"] and result["totals"]:
        w.writerow(["Total"] + ["" if v is None else v for v in result["totals"]])
    return buf.getvalue()
