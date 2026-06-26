"""Convert physical rows to/from JSON for the REST API.

Writes reuse the importer's value coercion (type/enum/regex rules + FK
resolution by id or display value), so the API validates exactly like CSV import.
"""
from datetime import date, datetime
from decimal import Decimal

from ..importer import _RelationResolver, coerce_value
from ..metadata.field_types import FILE_TYPES, RELATION_TYPE

# columns a client may never set (managed by record_service / the DB)
_MANAGED = {"id", "created_by", "created_at", "updated_by", "updated_at",
            "deleted_at", "deleted_by"}


class ApiError(ValueError):
    """A 400-level problem with the request body."""


def serialize_row(row, hide=None):
    """A dict row → JSON-safe values (datetime/date → ISO, Decimal → float).

    ``hide`` is a set of column names to omit (field-level read permissions).
    """
    hide = hide or set()
    out = {}
    for k, v in row.items():
        if k in hide:
            continue
        if isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


def deserialize(table, body, session, engine, *, partial, writable=None):
    """Validate/coerce a JSON body into a ``{column: value}`` dict.

    Rejects unknown keys, read-only columns, and file/image (virtual) fields. When
    ``writable`` is given (field-level permissions), keys outside it are rejected.
    On create (``partial=False``) every required field must be present.
    """
    if not isinstance(body, dict):
        raise ApiError("Request body must be a JSON object.")
    fields = {f.phys_name: f for f in table.fields if f.data_type not in FILE_TYPES}
    resolvers, values = {}, {}

    for key, val in body.items():
        if key in _MANAGED:
            raise ApiError(f"'{key}' is read-only.")
        f = fields.get(key)
        if not f:
            raise ApiError(f"Unknown field '{key}'.")
        if writable is not None and key not in writable:
            raise ApiError(f"'{key}' is read-only for your role.")
        if val is None or val == "":
            values[key] = None
            continue
        resolver = None
        if f.data_type == RELATION_TYPE:
            resolver = resolvers.setdefault(key, _RelationResolver(session, engine, f))
        try:
            values[key] = coerce_value(f, str(val), resolver)
        except ValueError as exc:
            raise ApiError(str(exc))

    if not partial:
        for name, f in fields.items():
            if not f.nullable and f.default_value in (None, "") and values.get(name) is None:
                raise ApiError(f"'{name}' is required.")
    return values
