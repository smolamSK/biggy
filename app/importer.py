"""CSV import: blank templates and bulk row loading into a table.

Insert or **upsert** (update existing rows matched on a key column — ``id`` or a
unique field — else insert). Handles scalar columns and many-to-one (FK)
columns; a relation cell may hold the related record's id or its display-field
value. Many-to-many links are out of scope (they are not columns of the table).
Like the rest of the importer this works at the :mod:`app.data_service` level,
so it does not stamp audit/owner columns.
"""
import csv
import io
import json
import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from . import data_service
from .forms.builder import display_field_name
from .metadata.field_types import FILE_TYPES, RELATION_TYPE
from .metadata.models import MetaTable

_TRUE = {"1", "true", "yes", "y", "t", "on"}
_FALSE = {"0", "false", "no", "n", "f", "off"}
_DT_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M")


def _to_float(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _check_rules(field, value):
    """Enforce a field's validation rules; raise ValueError on violation.

    Uses getattr so lightweight field stand-ins without rule attrs still work.
    """
    if field.data_type in ("string", "text"):
        minlen = getattr(field, "min_length", None)
        maxlen = getattr(field, "max_length", None)
        pattern = getattr(field, "pattern", None)
        if minlen and len(value) < minlen:
            raise ValueError(f"must be at least {minlen} characters")
        if maxlen and len(value) > maxlen:
            raise ValueError(f"must be at most {maxlen} characters")
        if pattern and not re.search(pattern, value):
            raise ValueError("does not match the required format")
    else:
        nmin = _to_float(getattr(field, "min_value", None))
        nmax = _to_float(getattr(field, "max_value", None))
        if nmin is not None and float(value) < nmin:
            raise ValueError(f"must be >= {getattr(field, 'min_value')}")
        if nmax is not None and float(value) > nmax:
            raise ValueError(f"must be <= {getattr(field, 'max_value')}")


def importable_fields(meta_table, allowed=None):
    """Columns that can be imported (scalar + relation FK), in display order.

    File/image fields are virtual (no column, no CSV value) and excluded. When
    ``allowed`` is given (field-level write permissions), others are excluded too.
    """
    return [f for f in meta_table.fields if f.data_type not in FILE_TYPES
            and (allowed is None or f.phys_name in allowed)]


def template_csv(meta_table):
    """A blank CSV (header row only) for the table."""
    buf = io.StringIO()
    csv.writer(buf).writerow([f.phys_name for f in importable_fields(meta_table)])
    return buf.getvalue()


class _RelationResolver:
    """Resolve a cell to a FK id by existing id or unique display value."""

    def __init__(self, session, engine, field):
        target = session.get(MetaTable, field.related_table_id)
        self.label = target.label if target else "record"
        disp = display_field_name(session, target)
        options = data_service.load_options(engine, target.phys_name, [disp])
        self.ids = {i for i, _ in options}
        self.by_label, self.dups = {}, set()
        for i, lbl in options:
            if lbl in self.by_label:
                self.dups.add(lbl)
            else:
                self.by_label[lbl] = i

    def resolve(self, raw):
        if raw.lstrip("-").isdigit() and int(raw) in self.ids:
            return int(raw)
        if raw in self.dups:
            raise ValueError(f"'{raw}' matches multiple {self.label} records")
        if raw in self.by_label:
            return self.by_label[raw]
        raise ValueError(f"no {self.label} matching '{raw}'")


def coerce_value(field, raw, resolver=None):
    """Coerce a raw CSV string to a Python value (empty -> None). Raises ValueError."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    dt = field.data_type
    name = field.phys_name
    try:
        if dt in ("string", "text"):
            _check_rules(field, raw)
            return raw
        if dt in ("integer", "bigint"):
            v = int(raw)
            _check_rules(field, v)
            return v
        if dt == "decimal":
            v = Decimal(raw)
            _check_rules(field, v)
            return v
        if dt == "float":
            v = float(raw)
            _check_rules(field, v)
            return v
        if dt == "boolean":
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError("expected yes/no")
        if dt == "date":
            return date.fromisoformat(raw)
        if dt == "datetime":
            return _parse_datetime(raw)
        if dt == "time":
            return time.fromisoformat(raw)
        if dt == "enum":
            opts = json.loads(field.enum_options or "[]")
            if raw in opts:
                return raw
            raise ValueError(f"expected one of {', '.join(opts)}")
        if dt in ("currency", "percent"):
            v = Decimal(raw)
            _check_rules(field, v)
            return v
        if dt == "email":
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", raw):
                raise ValueError("invalid email address")
            return raw
        if dt == "url":
            if not re.match(r"^https?://\S+$", raw):
                raise ValueError("invalid URL (http/https)")
            return raw
        if dt == "phone":
            if not re.match(r"^[+(]?[\d][\d\s().-]{4,}$", raw):
                raise ValueError("invalid phone number")
            return raw
        if dt == "autonumber":
            return raw
        if dt == "json":
            return json.dumps(json.loads(raw))      # validate + canonicalise
        if dt == "tags":
            opts = set(json.loads(field.enum_options or "[]"))
            parts = [p.strip() for p in re.split(r"[|,;]", raw) if p.strip()]
            unknown = [p for p in parts if p not in opts]
            if unknown:
                raise ValueError(f"unknown tag(s): {', '.join(unknown)}")
            return json.dumps(parts)
        if dt == RELATION_TYPE:
            return resolver.resolve(raw) if resolver else int(raw)
    except (ValueError, InvalidOperation) as exc:
        raise ValueError(f"{name}: {exc if str(exc) else 'invalid value'} ('{raw}')")
    raise ValueError(f"{name}: unsupported type {dt}")


def _parse_datetime(raw):
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError("expected a date/time")


def import_rows(session, engine, meta_table, file_text, skip_invalid,
                mode="insert", key_field=None, allowed=None):
    """Parse, validate and load rows. Returns a result summary.

    ``mode="upsert"`` matches each row on ``key_field`` (``"id"`` or a unique
    field): a hit updates that row, a miss inserts. ``mode="insert"`` (default)
    is insert-only and ignores ``key_field``. ``allowed`` restricts which columns
    may be written (field-level permissions).
    """
    fields = importable_fields(meta_table, allowed)
    names = {f.phys_name for f in fields}
    upsert = mode == "upsert" and bool(key_field)
    known = set(names) | ({"id"} if upsert and key_field == "id" else set())
    reader = csv.DictReader(io.StringIO(file_text))
    ignored = [h for h in (reader.fieldnames or []) if h and h not in known]
    resolvers = {
        f.phys_name: _RelationResolver(session, engine, f)
        for f in fields if f.data_type == RELATION_TYPE
    }

    inserts, updates, errors, total = [], [], [], 0
    for line_no, row in enumerate(reader, start=2):  # header occupies line 1
        total += 1
        values, row_errors = {}, []
        for f in fields:
            resolver = resolvers.get(f.phys_name)
            try:
                val = coerce_value(f, row.get(f.phys_name), resolver)
            except ValueError as exc:
                row_errors.append(str(exc))
                continue
            if val is None and not f.nullable:
                if f.default_value not in (None, ""):
                    try:
                        val = coerce_value(f, f.default_value, resolver)
                    except ValueError:
                        val = None
                if val is None:
                    row_errors.append(f"{f.phys_name}: required")
                    continue
            values[f.phys_name] = val

        existing_id = None
        if upsert and not row_errors:
            try:
                existing_id = _resolve_key(engine, meta_table, key_field, row, values)
            except ValueError as exc:
                row_errors.append(str(exc))

        if row_errors:
            errors.append((line_no, "; ".join(row_errors)))
        elif existing_id is not None:
            updates.append((existing_id, values))
        else:
            inserts.append(values)

    result = {"total": total, "imported": 0, "updated": 0,
              "errors": errors, "ignored_headers": ignored}
    if errors and not skip_invalid:
        return result
    try:
        if inserts:
            result["imported"] = data_service.insert_many(
                engine, meta_table.phys_name, inserts)
        for pk, vals in updates:
            data_service.update_row(engine, meta_table.phys_name, pk, vals)
            result["updated"] += 1
    except Exception as exc:  # noqa: BLE001 - surface DB errors to the user
        errors.append((0, f"Database error during import: {exc}"))
    return result


def _resolve_key(engine, meta_table, key_field, row, values):
    """Return the existing row id matching the upsert key, or None to insert."""
    if key_field == "id":
        raw = (row.get("id") or "").strip()
        key_val = int(raw) if raw.lstrip("-").isdigit() else None
    else:
        key_val = values.get(key_field)
    if key_val in (None, ""):
        return None
    return data_service.find_id_by(engine, meta_table.phys_name, key_field, key_val)
