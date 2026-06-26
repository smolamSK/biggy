"""Render a User-mode list (its visible columns) to CSV text.

Used by the "Export CSV" action (whole filtered result) and bulk "Export
selected". M:1 columns emit the human display label rather than the raw id so
the file round-trips back through :mod:`app.importer` (whose relation resolver
accepts an id *or* a display value).
"""
import csv
import io


def _cell(item, value, label_maps):
    if item.column in label_maps:
        if value is None:
            return ""
        return str(label_maps[item.column].get(value, value))
    if value is None:
        return ""
    if item.meta.data_type == "boolean":
        return "yes" if value else "no"
    return str(value)


def list_csv(columns, rows, label_maps):
    """Return CSV text for ``rows`` over the given list ``columns``."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    # physical column names as headers so the file re-imports cleanly
    writer.writerow([it.column for it in columns])
    for row in rows:
        writer.writerow([_cell(it, row.get(it.column), label_maps) for it in columns])
    return buf.getvalue()
