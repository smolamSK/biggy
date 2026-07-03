"""Generic CRUD/search over physical user tables.

Tables are reflected from the live database; every statement is built with the
SQLAlchemy expression language so all values are bound parameters. Column names
used for filtering/sorting are validated against the reflected table's columns.
"""
from sqlalchemy import MetaData, Table, delete, func, insert, select, update


def reflect_table(engine, phys_name):
    """Reflect and return a physical table (fresh metadata each call)."""
    md = MetaData()
    return Table(phys_name, md, autoload_with=engine)


def column_names(table):
    return [c.name for c in table.columns]


def _pk(table):
    """The table's single primary-key column (falls back to ``id`` / first column)."""
    pkcols = list(table.primary_key.columns)
    if pkcols:
        return pkcols[0]
    return table.c.get("id") if "id" in table.c else list(table.columns)[0]


def _pk_value(table, value):
    """Coerce a key (e.g. a string from a URL) to the PK column's Python type."""
    if value is None:
        return None
    try:
        pytype = _pk(table).type.python_type
    except (NotImplementedError, AttributeError):
        return value
    if isinstance(value, pytype):
        return value
    try:
        return pytype(value) if pytype in (int, str) else value
    except (TypeError, ValueError):
        return value


# --------------------------------------------------------------------------- #
# Row operations
# --------------------------------------------------------------------------- #
def insert_row(engine, phys_name, values):
    table = reflect_table(engine, phys_name)
    clean = {k: v for k, v in values.items() if k in table.c}
    with engine.begin() as conn:
        result = conn.execute(insert(table).values(**clean))
        # supplied (natural) key, else the generated one
        pkname = _pk(table).name
        if pkname in clean:
            return clean[pkname]
        return result.inserted_primary_key[0]


def insert_many(engine, phys_name, rows):
    """Insert many rows in a single transaction (all-or-nothing). Returns count."""
    table = reflect_table(engine, phys_name)
    cleaned = [{k: v for k, v in row.items() if k in table.c} for row in rows]
    if not cleaned:
        return 0
    with engine.begin() as conn:
        conn.execute(insert(table), cleaned)
    return len(cleaned)


def update_row(engine, phys_name, pk, values):
    table = reflect_table(engine, phys_name)
    pkcol = _pk(table)
    clean = {k: v for k, v in values.items() if k in table.c and k != pkcol.name}
    with engine.begin() as conn:
        conn.execute(update(table).where(pkcol == _pk_value(table, pk)).values(**clean))


def delete_row(engine, phys_name, pk):
    table = reflect_table(engine, phys_name)
    pkcol = _pk(table)
    with engine.begin() as conn:
        conn.execute(delete(table).where(pkcol == _pk_value(table, pk)))


def get_row(engine, phys_name, pk):
    table = reflect_table(engine, phys_name)
    pkcol = _pk(table)
    with engine.connect() as conn:
        row = conn.execute(
            select(table).where(pkcol == _pk_value(table, pk))).mappings().first()
        return dict(row) if row else None


def list_rows_after(engine, phys_name, after_id, limit=500):
    """Rows with PK greater than ``after_id`` (ascending) — used by scheduled feeds."""
    table = reflect_table(engine, phys_name)
    pkcol = _pk(table)
    with engine.connect() as conn:
        rows = conn.execute(
            select(table).where(pkcol > _pk_value(table, after_id))
            .order_by(pkcol).limit(limit)).mappings().all()
        return [dict(r) for r in rows]


def count_rows(engine, phys_name, col, value):
    """Count rows where ``col == value`` (0 if the column is missing)."""
    table = reflect_table(engine, phys_name)
    if col not in table.c:
        return 0
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(table).where(table.c[col] == value)
        ).scalar_one()


def column_nullable(engine, phys_name, col):
    table = reflect_table(engine, phys_name)
    return bool(table.c[col].nullable) if col in table.c else True


def clear_fk(engine, phys_name, col, value):
    """Set ``col`` to NULL on every row where it currently equals ``value``."""
    table = reflect_table(engine, phys_name)
    if col not in table.c:
        return
    with engine.begin() as conn:
        conn.execute(update(table).where(table.c[col] == value).values(**{col: None}))


def repoint_fk(engine, phys_name, col, old_value, new_value):
    """Repoint ``col`` from ``old_value`` to ``new_value``; returns rows changed."""
    table = reflect_table(engine, phys_name)
    if col not in table.c:
        return 0
    with engine.begin() as conn:
        res = conn.execute(update(table).where(table.c[col] == old_value)
                           .values(**{col: new_value}))
        return res.rowcount


def delete_where(engine, phys_name, col, value):
    """Delete rows where ``col == value`` (junction cleanup / required children)."""
    table = reflect_table(engine, phys_name)
    if col not in table.c:
        return
    with engine.begin() as conn:
        conn.execute(delete(table).where(table.c[col] == value))


def _clause_for(table, f):
    """One condition dict -> a boolean clause (or None when invalid/empty)."""
    from .filters import NO_VALUE_OPS, build_clause

    col = f.get("col")
    if col not in table.c:
        return None
    op = f.get("op") or "contains"
    value = f.get("value")
    if op not in NO_VALUE_OPS and value in (None, ""):
        return None
    return build_clause(table.c[col], op, value, is_text=bool(f.get("is_text")))


def _apply_filters(stmt, table, filters):
    from sqlalchemy import or_

    for f in filters or []:
        if "any" in f:                       # OR-group: match any sub-condition
            clauses = [c for c in (_clause_for(table, sub) for sub in f["any"] or [])
                       if c is not None]
            if clauses:
                stmt = stmt.where(or_(*clauses))
            continue
        clause = _clause_for(table, f)
        if clause is not None:
            stmt = stmt.where(clause)
    return stmt


def list_rows(engine, phys_name, *, filters=None, sort=None, order="asc",
              page=1, per_page=25):
    """Return ``(rows, total)``; rows are dicts, paginated and optionally filtered.

    ``per_page=None`` returns every matching row (no LIMIT/OFFSET) — used by
    CSV export of a filtered list.
    """
    table = reflect_table(engine, phys_name)
    base = _apply_filters(select(table), table, filters)

    count_stmt = _apply_filters(
        select(func.count()).select_from(table), table, filters
    )

    if sort and sort in table.c:
        column = table.c[sort]
        base = base.order_by(column.desc() if order == "desc" else column.asc())
    else:
        base = base.order_by(_pk(table).asc())

    if per_page is not None:
        page = max(1, int(page))
        base = base.limit(per_page).offset((page - 1) * per_page)

    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(base).mappings().all()]
        total = conn.execute(count_stmt).scalar_one()
    return rows, total


_AGG = {"count": func.count, "sum": func.sum, "avg": func.avg,
        "min": func.min, "max": func.max}


def _metric_expr(table, m):
    """Build a labelled aggregate expression for one metric, or None if invalid."""
    fn = _AGG.get(m.get("func"))
    if fn is None:
        return None
    col = m.get("col")
    if m["func"] == "count" and not col:
        expr = func.count()
    elif col in table.c:
        expr = fn(table.c[col])
    else:
        return None
    return expr.label(m["label"])


def aggregate_rows(engine, phys_name, group_col, metrics, *,
                   filters=None, order=None, limit=1000):
    """Run a GROUP BY aggregation. Returns ``(labels, rows, totals_row)``.

    ``group_col`` is a physical column (or None for a single total). ``metrics``
    are dicts ``{func, col, label}`` (``func`` in count/sum/avg/min/max). All
    column names are validated against the reflected table; ``filters`` reuse the
    list-view operators. ``totals_row`` is the same metrics with no GROUP BY.
    """
    table = reflect_table(engine, phys_name)
    metric_exprs, labels, by_label = [], [], {}
    grp = table.c[group_col] if (group_col and group_col in table.c) else None
    if grp is not None:
        labels.append(group_col)
    for m in metrics:
        expr = _metric_expr(table, m)
        if expr is not None:
            metric_exprs.append(expr)
            labels.append(m["label"])
            by_label[m["label"]] = expr

    sel = ([grp] if grp is not None else []) + metric_exprs
    base = _apply_filters(select(*sel), table, filters).select_from(table)
    if grp is not None:
        base = base.group_by(grp)
        if order in by_label:
            base = base.order_by(by_label[order].desc())
        elif metric_exprs:
            base = base.order_by(metric_exprs[0].desc())
        else:
            base = base.order_by(grp.asc())
        if limit:
            base = base.limit(limit)

    with engine.connect() as conn:
        rows = [list(r) for r in conn.execute(base).all()]
        totals = None
        if metric_exprs:
            tstmt = _apply_filters(select(*metric_exprs), table, filters).select_from(table)
            trow = conn.execute(tstmt).first()
            totals = list(trow) if trow else None
    return labels, rows, totals


def aggregate_value(engine, phys_name, op, col=None, *, where_col=None, where_val=None):
    """One aggregate (count/sum/avg/min/max), optionally filtered by ``where_col``.

    Column names are validated against the reflected table; returns the scalar
    (int for count, Decimal/None for the rest) or ``None`` for an invalid request.
    """
    table = reflect_table(engine, phys_name)
    fn = _AGG.get(op)
    if fn is None:
        return None
    if op == "count":
        expr = func.count(table.c[col]) if (col and col in table.c) else func.count()
    elif col in table.c:
        expr = fn(table.c[col])
    else:
        return None
    stmt = select(expr)
    if where_col and where_col in table.c:
        stmt = stmt.where(table.c[where_col] == where_val)
    with engine.connect() as conn:
        return conn.execute(stmt).scalar()


def rows_by_ids(engine, phys_name, ids):
    """Return rows (dicts) whose id is in ``ids``, ordered by id."""
    if not ids:
        return []
    table = reflect_table(engine, phys_name)
    ids = [_pk_value(table, i) for i in ids]
    stmt = select(table).where(_pk(table).in_(ids)).order_by(_pk(table).asc())
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(stmt).mappings().all()]


def _match_clause(column, value, normalize):
    """``column == value``, optionally case-insensitive + whitespace-trimmed."""
    if normalize and isinstance(value, str):
        return func.lower(func.trim(column)) == value.strip().lower()
    return column == value


def find_id_by(engine, phys_name, col, value, *, normalize=False):
    """Return the single id where ``col == value`` (None if none).

    ``col``/``value`` may be lists of equal length — a **composite** key where all
    parts must match. ``normalize=True`` compares strings case-insensitively and
    whitespace-trimmed (reconciliation-friendly). Raises ValueError if a column is
    missing or the match is ambiguous (>1).
    """
    table = reflect_table(engine, phys_name)
    cols = col if isinstance(col, (list, tuple)) else [col]
    values = value if isinstance(value, (list, tuple)) else [value]
    for c in cols:
        if c not in table.c:
            raise ValueError(f"unknown column '{c}'")
    stmt = select(_pk(table)).where(
        *[_match_clause(table.c[c], v, normalize) for c, v in zip(cols, values)]).limit(2)
    with engine.connect() as conn:
        found = [r[0] for r in conn.execute(stmt).all()]
    if len(found) > 1:
        raise ValueError(f"'{value}' matches multiple rows on {col}")
    return found[0] if found else None


def find_id_by_key(engine, phys_name, key_spec, values, *, normalize=True):
    """Upsert-key lookup shared by webhooks / pulls / the importer.

    ``key_spec`` is a column name or a comma-separated **composite** ("serial,site").
    Returns None (→ insert) unless every key part has a non-None value in ``values``;
    matching is normalized (case-insensitive, trimmed) by default.
    """
    keys = [k.strip() for k in (key_spec or "").split(",") if k.strip()]
    if not keys or any(values.get(k) in (None, "") for k in keys):
        return None
    return find_id_by(engine, phys_name, keys, [values[k] for k in keys],
                      normalize=normalize)


# --------------------------------------------------------------------------- #
# Relation helpers
# --------------------------------------------------------------------------- #
LABEL_SEP = " — "


def _as_columns(table, display_cols):
    """Resolve a display-column spec (str or list) to existing table columns."""
    if isinstance(display_cols, str):
        display_cols = [display_cols]
    cols = [table.c[c] for c in (display_cols or []) if c in table.c]
    return cols or [_pk(table)]


def _compose_label(pk, values):
    parts = [str(v) for v in values if v not in (None, "")]
    return LABEL_SEP.join(parts) if parts else f"#{pk}"


def load_options(engine, phys_name, display_cols):
    """Return ``[(id, label), ...]`` for a FK/relation picker.

    ``display_cols`` is a column name or list of names; multiple are composed
    into one label (e.g. ``"Acme — a@acme.test"``).
    """
    table = reflect_table(engine, phys_name)
    cols = _as_columns(table, display_cols)
    stmt = select(_pk(table), *cols).order_by(cols[0].asc())
    with engine.connect() as conn:
        return [(r[0], _compose_label(r[0], r[1:])) for r in conn.execute(stmt).all()]


def load_options_with(engine, phys_name, display_cols, extra_col):
    """Like :func:`load_options` but each tuple also carries ``extra_col``'s value.

    Returns ``[(id, label, extra_value), ...]`` — used for cascading pickers
    (e.g. each contact's ``company_id``). ``extra_value`` is None if the column
    is missing.
    """
    table = reflect_table(engine, phys_name)
    cols = _as_columns(table, display_cols)
    has_extra = extra_col in table.c
    selected = [_pk(table), *cols] + ([table.c[extra_col]] if has_extra else [])
    stmt = select(*selected).order_by(cols[0].asc())
    n = 1 + len(cols)
    with engine.connect() as conn:
        return [(r[0], _compose_label(r[0], r[1:n]), (r[n] if has_extra else None))
                for r in conn.execute(stmt).all()]


def sample_labels(engine, phys_name, where_col, where_val, display_cols, limit=25):
    """Composed labels for rows where ``where_col == where_val`` (capped)."""
    table = reflect_table(engine, phys_name)
    if where_col not in table.c:
        return []
    cols = _as_columns(table, display_cols)
    stmt = (select(_pk(table), *cols).where(table.c[where_col] == where_val)
            .order_by(_pk(table)).limit(limit))
    with engine.connect() as conn:
        return [_compose_label(r[0], r[1:]) for r in conn.execute(stmt).all()]


def labels_for(engine, phys_name, ids, display_cols):
    """Composed labels for the given ids, preserving order."""
    if not ids:
        return []
    table = reflect_table(engine, phys_name)
    cols = _as_columns(table, display_cols)
    with engine.connect() as conn:
        found = {r[0]: _compose_label(r[0], r[1:])
                 for r in conn.execute(select(_pk(table), *cols)
                                       .where(_pk(table).in_(list(ids)))).all()}
    return [found[i] for i in ids if i in found]


def get_links(engine, jname, this_col, this_id, other_col):
    """Return the list of related ids for a many-to-many record."""
    table = reflect_table(engine, jname)
    stmt = select(table.c[other_col]).where(table.c[this_col] == this_id)
    with engine.connect() as conn:
        return [r[0] for r in conn.execute(stmt).all()]


def _coerce_to(col, value):
    try:
        pytype = col.type.python_type
        return pytype(value) if pytype in (int, str) and not isinstance(value, pytype) else value
    except (NotImplementedError, AttributeError, TypeError, ValueError):
        return value


def set_links(engine, jname, this_col, this_id, other_col, other_ids):
    """Replace the set of many-to-many links for a record."""
    table = reflect_table(engine, jname)
    wanted = {_coerce_to(table.c[other_col], x) for x in other_ids}
    with engine.begin() as conn:
        existing = {
            r[0] for r in conn.execute(
                select(table.c[other_col]).where(table.c[this_col] == this_id)
            ).all()
        }
        for rid in wanted - existing:
            conn.execute(insert(table).values(**{this_col: this_id, other_col: rid}))
        to_remove = existing - wanted
        if to_remove:
            conn.execute(
                delete(table).where(
                    table.c[this_col] == this_id,
                    table.c[other_col].in_(to_remove),
                )
            )
