"""Generate and execute DDL for physical user tables from field metadata.

All identifiers here come from validated metadata (see :mod:`app.identifiers`)
and are emitted through SQLAlchemy ``Table``/``Column`` objects (CREATE) or
Alembic operations (ALTER), which render per dialect — so the same code runs on
MariaDB/MySQL, PostgreSQL and SQLite. User-supplied *values* never reach DDL.
"""
import json

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    false,
    inspect,
    text,
    true,
)

from . import ddl
from .field_types import FILE_TYPES, RELATION_TYPE

# data_type values that do NOT map to a physical column
_VIRTUAL_TYPES = {RELATION_TYPE} | set(FILE_TYPES)

ON_DELETE_CHOICES = ("SET NULL", "CASCADE", "RESTRICT")


# --------------------------------------------------------------------------- #
# Type mapping
# --------------------------------------------------------------------------- #
def sa_type_for_field(field):
    """Map a :class:`MetaField` (scalar) to a SQLAlchemy column type."""
    dt = field.data_type
    if dt == "string":
        return String(field.length or 255)
    if dt in ("text", "markdown"):
        return Text()
    if dt == "integer":
        return Integer()
    if dt == "bigint":
        return BigInteger()
    if dt == "decimal":
        return Numeric(field.precision or 12, field.scale if field.scale is not None else 2)
    if dt == "float":
        return Float()
    if dt == "boolean":
        return Boolean()
    if dt == "date":
        return Date()
    if dt == "datetime":
        return DateTime()
    if dt == "time":
        return Time()
    if dt == "enum":
        opts = json.loads(field.enum_options or "[]")
        # non-native: a CHECK-constrained VARCHAR — portable across dialects and
        # avoids the PostgreSQL ENUM-type lifecycle.
        return Enum(*opts, native_enum=False) if opts else String(255)
    if dt in ("email", "url"):
        return String(field.length or 255)
    if dt == "phone":
        return String(field.length or 40)
    if dt in ("currency", "percent"):
        return Numeric(field.precision or 12, field.scale if field.scale is not None else 2)
    if dt in ("json", "tags"):
        return Text()
    if dt == "autonumber":
        return String(field.length or 32)
    if dt == "formula":
        rt = field.result_type or "number"
        if rt == "text":
            return Text()
        if rt == "boolean":
            return Boolean()
        if rt == "date":
            return Date()
        if rt == "datetime":
            return DateTime()
        return Numeric(field.precision or 18, field.scale if field.scale is not None else 4)
    raise ValueError(f"Unsupported scalar data type: {dt!r}")


def _scalar_column(field, *, unique=None):
    return Column(
        field.phys_name,
        sa_type_for_field(field),
        nullable=field.nullable,
        unique=bool(field.is_unique) if unique is None else unique,
    )


def _uq_name(table_phys, col):
    return f"uq_{table_phys}_{col}"[:64]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _preparer(engine):
    return engine.dialect.identifier_preparer


def table_exists(engine, phys_name):
    return inspect(engine).has_table(phys_name)


def _reflect_pk(engine, phys_name, md=None):
    """Reflect a table and return its single primary-key Column."""
    tbl = Table(phys_name, md or MetaData(), autoload_with=engine)
    pkcols = list(tbl.primary_key.columns)
    return pkcols[0] if pkcols else tbl.c.id


# Columns added to existing app_meta_* tables after the initial release. Stored as
# specs and applied idempotently via reflection (no MySQL-only IF NOT EXISTS).
_META_ADDITIONS = {
    "app_user": [
        {"name": "totp_secret", "type": Text()},
        {"name": "mfa_enabled", "type": Boolean(), "nullable": False, "default": "false"},
        {"name": "mfa_backup_codes", "type": Text()},
        {"name": "oidc_subject", "type": String(255)},
    ],
    "app_meta_relation": [
        {"name": "to_display_field_ids", "type": Text()},
        {"name": "from_display_field_ids", "type": Text()},
    ],
    "app_meta_field": [
        {"name": "min_length", "type": Integer()},
        {"name": "max_length", "type": Integer()},
        {"name": "min_value", "type": String(64)},
        {"name": "max_value", "type": String(64)},
        {"name": "pattern", "type": String(255)},
        {"name": "formula", "type": Text()},
        {"name": "result_type", "type": String(20)},
        {"name": "enum_colors", "type": Text()},
    ],
    "app_meta_table": [
        {"name": "track_audit", "type": Boolean(), "nullable": False, "default": "false"},
        {"name": "soft_delete", "type": Boolean(), "nullable": False, "default": "false"},
        {"name": "row_owned", "type": Boolean(), "nullable": False, "default": "false"},
        {"name": "managed", "type": Boolean(), "nullable": False, "default": "true"},
        {"name": "source_id", "type": Integer()},
        {"name": "pk_col", "type": String(64), "nullable": False, "default": "'id'"},
    ],
    "app_meta_form_field": [
        {"name": "parent_field_id", "type": Integer()},
        {"name": "filter_field_id", "type": Integer()},
    ],
    "app_meta_menu": [
        {"name": "target_dashboard_id", "type": Integer()},
    ],
    "app_meta_form": [
        {"name": "purpose", "type": String(10), "nullable": False, "default": "'data'"},
        {"name": "in_catalog", "type": Boolean(), "nullable": False, "default": "false"},
        {"name": "catalog_group", "type": String(80)},
        {"name": "default_sort", "type": String(64)},
        {"name": "default_order", "type": String(4)},
        {"name": "default_per_page", "type": Integer()},
        {"name": "portal_close_state", "type": String(64)},
    ],
    "app_report": [
        {"name": "pinned", "type": Boolean(), "nullable": False, "default": "false"},
        {"name": "schedule_minutes", "type": Integer()},
        {"name": "recipients", "type": String(400)},
        {"name": "last_run_at", "type": DateTime()},
    ],
    "app_webhook": [
        {"name": "max_body_bytes", "type": Integer()},
        {"name": "rate_limit", "type": Integer()},
        {"name": "rate_window", "type": Integer()},
    ],
    "app_trigger_rule": [
        {"name": "schedule_minutes", "type": Integer()},
        {"name": "last_run_at", "type": DateTime()},
        {"name": "create_table_id", "type": Integer()},
        {"name": "create_field_map", "type": Text()},
        {"name": "webhook_format", "type": String(10)},
    ],
    "app_sla_policy": [
        {"name": "escalations", "type": Text()},
    ],
    "app_sla_clock": [
        {"name": "escalation_level", "type": Integer(), "nullable": False, "default": "0"},
    ],
    "app_pull_source": [
        {"name": "config", "type": Text()},
        {"name": "auth_secret", "type": Text()},   # encrypted at rest (ciphertext > 255 chars)
        {"name": "schedule_minutes", "type": Integer()},
    ],
}

# Columns widened to TEXT after the initial release (now hold encrypted ciphertext,
# which exceeds the original VARCHAR length). Applied idempotently after the adds.
_META_WIDENINGS = [
    ("app_connection", "token"),
    ("app_data_source", "password"),
    ("app_webhook", "secret"),
    ("app_pull_source", "auth_secret"),
]


def _spec_col(spec):
    """Build a fresh Column from an addition spec (fresh each call for Alembic)."""
    kw = {}
    if not spec.get("nullable", True):
        kw["nullable"] = False
    d = spec.get("default")
    if d == "false":
        kw["server_default"] = false()
    elif d == "true":
        kw["server_default"] = true()
    elif d is not None:
        kw["server_default"] = text(d)
    return Column(spec["name"], spec["type"], **kw)


def ensure_meta_schema(engine):
    """Add any metadata columns introduced after a database was first created."""
    for table, specs in _META_ADDITIONS.items():
        if not table_exists(engine, table):
            continue
        for spec in specs:
            ddl.add_column_if_missing(engine, table, _spec_col(spec))
    # widen secret columns to TEXT on existing DBs (they now hold encrypted ciphertext)
    for table, column in _META_WIDENINGS:
        if table_exists(engine, table):
            ddl.widen_to_text(engine, table, column)
    ensure_indexes(engine)


def ensure_indexes(engine):
    """Create any declared ``app_*`` index missing from an existing database.

    ``create_all`` only makes indexes together with *new* tables; this backfills
    indexes added to models after a table was first created.
    """
    from .models import Base

    for table in Base.metadata.tables.values():
        if not table_exists(engine, table.name):
            continue
        for index in table.indexes:
            ddl.create_index_if_missing(engine, index)


# audit / soft-delete columns added to a data table when its flags are enabled
AUDIT_COLS = ("created_by", "created_at", "updated_by", "updated_at")
SOFT_DELETE_COLS = ("deleted_at", "deleted_by")


def ensure_record_columns(engine, meta_table):
    """Idempotently add audit / soft-delete columns for an enabled table."""
    cols = []
    if meta_table.track_audit or meta_table.row_owned:
        cols += [Column("created_by", Integer()), Column("created_at", DateTime()),
                 Column("updated_by", Integer()), Column("updated_at", DateTime())]
    if meta_table.soft_delete:
        cols += [Column("deleted_at", DateTime()), Column("deleted_by", Integer())]
    for col in cols:
        ddl.add_column_if_missing(engine, meta_table.phys_name, col)


def build_data_table(phys_name, fields, pk=None, metadata=None):
    """Build a SQLAlchemy ``Table`` for CREATE TABLE.

    Default: an auto-increment integer ``id`` PK. Pass ``pk`` (a MetaField) to use
    a custom/natural primary key (that column, non-auto-increment). Relation (FK)
    columns are added afterwards via :func:`add_relation_column`.
    """
    md = metadata or MetaData()
    if pk is None:
        cols = [Column("id", Integer, primary_key=True, autoincrement=True)]
        pk_name = "id"
    else:
        cols = [Column(pk.phys_name, sa_type_for_field(pk), primary_key=True,
                       autoincrement=False)]
        pk_name = pk.phys_name
    for f in fields:
        if f.data_type in _VIRTUAL_TYPES or f.phys_name == pk_name:
            continue
        cols.append(_scalar_column(f))
    return Table(phys_name, md, *cols, mysql_engine="InnoDB")


# --------------------------------------------------------------------------- #
# DDL operations (ALTER via Alembic → portable across dialects)
# --------------------------------------------------------------------------- #
def create_physical_table(engine, phys_name, fields, pk=None):
    build_data_table(phys_name, fields, pk=pk).create(engine)


def drop_physical_table(engine, phys_name):
    q = _preparer(engine).quote
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {q(phys_name)}"))


def add_scalar_column(engine, table_phys, field):
    """Add a scalar column (+ a UNIQUE index if requested)."""
    col = _scalar_column(field, unique=False)
    with engine.begin() as conn:
        ddl.operations(conn).add_column(table_phys, col)
    if field.is_unique:
        with engine.begin() as conn:
            ddl.operations(conn).create_index(
                _uq_name(table_phys, field.phys_name), table_phys, [field.phys_name], unique=True)


def add_relation_column(engine, from_table, field, to_table):
    """Add a many-to-one FK column to ``from_table`` referencing the target's PK.

    The FK column takes the target primary key's name/type (not always integer
    ``id``), so relations work against tables with an arbitrary single-column PK.
    """
    tpk = _reflect_pk(engine, to_table)
    on_delete = (field.on_delete or "SET NULL").upper()
    nullable = field.nullable or on_delete == "SET NULL"
    fk_name = f"fk_{from_table}_{field.phys_name}"[:64]
    with engine.begin() as conn:
        with ddl.operations(conn).batch_alter_table(from_table) as batch:
            batch.add_column(Column(field.phys_name, tpk.type, nullable=nullable))
            batch.create_foreign_key(fk_name, to_table, [field.phys_name], [tpk.name],
                                     ondelete=on_delete)


def drop_column(engine, table_phys, column_phys):
    """Drop a column; first drop any FK / single-column UNIQUE on it (batch-safe).

    On SQLite the batch rebuild otherwise tries to re-create the column's index.
    """
    fk_names = [fk["name"] for fk in inspect(engine).get_foreign_keys(table_phys)
                if column_phys in (fk.get("constrained_columns") or []) and fk.get("name")]
    uq_name, uq_kind = _old_unique(engine, table_phys, column_phys)
    with engine.begin() as conn:
        with ddl.operations(conn).batch_alter_table(table_phys) as batch:
            if uq_kind == "unique":
                batch.drop_constraint(uq_name, type_="unique")
            elif uq_kind == "index":
                batch.drop_index(uq_name)
            for fkn in fk_names:
                batch.drop_constraint(fkn, type_="foreignkey")
            batch.drop_column(column_phys)


def _old_unique(engine, table_phys, col):
    """(name, kind) of an existing single-column UNIQUE on ``col``, or (None, None)."""
    insp = inspect(engine)
    for uc in insp.get_unique_constraints(table_phys):
        if uc.get("column_names") == [col] and uc.get("name"):
            return uc["name"], "unique"
    for ix in insp.get_indexes(table_phys):
        if ix.get("unique") and ix.get("column_names") == [col] and ix.get("name"):
            return ix["name"], "index"
    return None, None


def modify_column(engine, table_phys, old_name, field):
    """Rename/retype/renull a scalar column and sync its UNIQUE."""
    uq_name, uq_kind = _old_unique(engine, table_phys, old_name)
    new_type = sa_type_for_field(field)
    with engine.begin() as conn:
        op = ddl.operations(conn)
        with op.batch_alter_table(table_phys) as batch:
            if uq_kind == "unique":
                batch.drop_constraint(uq_name, type_="unique")
            elif uq_kind == "index":
                batch.drop_index(uq_name)
            batch.alter_column(old_name, new_column_name=field.phys_name,
                               type_=new_type, nullable=field.nullable)
        if field.is_unique:
            op.create_index(_uq_name(table_phys, field.phys_name), table_phys,
                            [field.phys_name], unique=True)


def add_composite_unique(engine, table_phys, name, cols):
    """Add a named multi-column UNIQUE (a unique index — portable)."""
    with engine.begin() as conn:
        ddl.operations(conn).create_index(name, table_phys, list(cols), unique=True)


def drop_composite_unique(engine, table_phys, name):
    with engine.begin() as conn:
        ddl.operations(conn).drop_index(name, table_name=table_phys)


def create_junction_table(engine, jname, left_table, left_col, right_table, right_col):
    """Create a many-to-many junction with two cascading FKs to each side's PK."""
    md = MetaData()
    lt = Table(left_table, md, autoload_with=engine)
    rt = lt if right_table == left_table else Table(right_table, md, autoload_with=engine)
    lpk, rpk = list(lt.primary_key.columns)[0], list(rt.primary_key.columns)[0]
    jt = Table(
        jname,
        md,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column(left_col, lpk.type,
               ForeignKey(f"{left_table}.{lpk.name}", ondelete="CASCADE"), nullable=False),
        Column(right_col, rpk.type,
               ForeignKey(f"{right_table}.{rpk.name}", ondelete="CASCADE"), nullable=False),
        UniqueConstraint(left_col, right_col, name=f"uq_{jname}"[:64]),
        mysql_engine="InnoDB",
    )
    jt.create(engine)
