"""Validation and sanitisation of SQL identifiers (table & column names).

SECURITY: every physical table/column name originates here. Names are validated
on creation and only ever sourced from stored metadata thereafter; they are passed
to the database exclusively through SQLAlchemy ``Table``/``Column`` objects (which
quote them), never string-interpolated from raw request input.
"""
import re

# MariaDB allows 64 chars; keep margin so junction names ``j_<a>_<b>`` fit.
MAX_LEN = 60
RESERVED_PREFIXES = ("app_", "j_")
# physical column names the app manages itself (PK + audit/soft-delete)
RESERVED_COLUMNS = frozenset({
    "id", "created_by", "created_at", "updated_by", "updated_at",
    "deleted_at", "deleted_by",
})

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class IdentifierError(ValueError):
    """Raised when a proposed identifier is invalid."""


def normalize(name):
    """Lower-case and trim a proposed identifier."""
    return (name or "").strip().lower()


def validate_identifier(name, *, kind="identifier", allow_reserved=False):
    """Return a normalized, validated identifier or raise :class:`IdentifierError`.

    Identifiers must match ``^[a-z][a-z0-9_]*$`` and be at most ``MAX_LEN`` chars.
    Unless ``allow_reserved`` is set, names may not start with a reserved prefix
    (``app_`` for metadata tables, ``j_`` for junction tables).
    """
    norm = normalize(name)
    if not norm:
        raise IdentifierError(f"{kind} name is required.")
    if len(norm) > MAX_LEN:
        raise IdentifierError(f"{kind} name must be at most {MAX_LEN} characters.")
    if not _IDENT_RE.match(norm):
        raise IdentifierError(
            f"{kind} name must start with a letter and contain only "
            "lower-case letters, digits and underscores."
        )
    if not allow_reserved and norm.startswith(RESERVED_PREFIXES):
        raise IdentifierError(
            f"{kind} name may not start with a reserved prefix "
            f"({', '.join(RESERVED_PREFIXES)})."
        )
    return norm


def sanitize_identifier(raw, *, kind="identifier", fallback="col"):
    """Derive a valid identifier from arbitrary text (e.g. a CSV header).

    Lower-cases, maps runs of non-alphanumerics to ``_``, prefixes a letter when
    needed, trims to :data:`MAX_LEN`, dodges reserved names/prefixes, then runs
    :func:`validate_identifier` so the result is guaranteed safe.
    """
    norm = re.sub(r"[^a-z0-9]+", "_", normalize(raw)).strip("_")
    if not norm:
        norm = fallback
    if not norm[0].isalpha():
        norm = f"c_{norm}"
    norm = norm[:MAX_LEN]
    if norm in RESERVED_COLUMNS or norm.startswith(RESERVED_PREFIXES):
        norm = f"x_{norm}"[:MAX_LEN]
    return validate_identifier(norm, kind=kind)


def junction_name(table_a, table_b):
    """Deterministic junction-table name for a many-to-many relation."""
    a, b = sorted([table_a, table_b])
    name = f"j_{a}_{b}"
    if len(name) > 64:
        name = name[:64]
    return name
