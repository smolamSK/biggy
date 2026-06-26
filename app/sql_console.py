"""Read-only SQL console: validate, run, and CSV-export a single SELECT.

Designer-only. Only one ``SELECT`` / ``WITH … SELECT`` is allowed; the query is
run without committing (never writes), and stacked statements / file exports are
rejected. This protects the app's own data and metadata tables.
"""
import csv
import io
import re

EXPORT_CAP = 100000
_LEADING_COMMENTS = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.S)
_OUTFILE = re.compile(r"\binto\s+(outfile|dumpfile)\b", re.I)


def validate_select(sql):
    """Return (clean_sql, None) for an allowed read-only query, else (None, error)."""
    s = (sql or "").strip()
    s = _LEADING_COMMENTS.sub("", s, count=1).strip().rstrip(";").strip()
    if not s:
        return None, "Enter a query."
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return None, "Only SELECT (or WITH … SELECT) queries are allowed."
    if ";" in s:
        return None, "Only a single statement is allowed."
    if _OUTFILE.search(s):
        return None, "INTO OUTFILE / DUMPFILE is not allowed."
    return s, None


def run_query(engine, sql, limit=500):
    """Execute a validated SELECT read-only. Returns (columns, rows, truncated)."""
    with engine.connect() as conn:
        result = conn.exec_driver_sql(sql)   # raw: don't treat ':' as a bind param
        columns = list(result.keys())
        fetched = result.fetchmany(limit + 1) if limit else result.fetchall()
        conn.rollback()                      # never commit
    truncated = bool(limit) and len(fetched) > limit
    rows = fetched[:limit] if limit else fetched
    return columns, [list(r) for r in rows], truncated


def to_csv(columns, rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return buf.getvalue()
