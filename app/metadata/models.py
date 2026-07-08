"""ORM models for the application's own metadata (prefixed ``app_``).

These describe *definitions* (tables, fields, relations, forms, menus) and the
user accounts. The actual user data lives in physical tables created by
:mod:`app.metadata.schema_service` and accessed via :mod:`app.data_service`.
"""
from datetime import datetime, timezone

from flask_login import UserMixin
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    backref,
    mapped_column,
    relationship,
)
from werkzeug.security import check_password_hash, generate_password_hash

from ..crypto import EncryptedText


def _utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
ROLE_DESIGNER = "designer"
ROLE_USER = "user"
ROLE_PORTAL = "portal"     # external customer: /portal only (catalog + own tickets)
ROLES = (ROLE_DESIGNER, ROLE_USER, ROLE_PORTAL)


class AppUser(Base, UserMixin):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)
    is_active_flag: Mapped[bool] = mapped_column("is_active", Boolean, default=True)
    # TOTP two-factor (see app/totp.py). Secret encrypted at rest; backup codes hashed.
    totp_secret: Mapped[str | None] = mapped_column(EncryptedText)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_backup_codes: Mapped[str | None] = mapped_column(Text)  # JSON list of sha256 hashes
    oidc_subject: Mapped[str | None] = mapped_column(String(255), unique=True)  # linked SSO 'sub'
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):  # Flask-Login hook
        return bool(self.is_active_flag)

    @property
    def is_designer(self):
        return self.role == ROLE_DESIGNER

    @property
    def is_portal(self):
        return self.role == ROLE_PORTAL


# --------------------------------------------------------------------------- #
# Schema definitions
# --------------------------------------------------------------------------- #
class MetaTable(Base):
    __tablename__ = "app_meta_table"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phys_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # column used as the human label when this table is referenced (FK pickers)
    display_field_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    # per-table behaviour
    track_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    soft_delete: Mapped[bool] = mapped_column(Boolean, default=False)
    row_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    # False for an *adopted* external table — Biggy maps it but never alters/drops
    # its schema (no CREATE/ALTER/DROP). True for tables Biggy created itself.
    managed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # which database the rows live in; NULL = the home database (where metadata is)
    source_id: Mapped[int | None] = mapped_column(Integer)
    # name of the single primary-key column (default "id"); may differ for adopted
    # tables or a custom/natural key chosen at creation.
    pk_col: Mapped[str] = mapped_column(String(64), default="id", nullable=False)

    fields: Mapped[list["MetaField"]] = relationship(
        back_populates="table",
        cascade="all, delete-orphan",
        order_by="MetaField.position",
    )
    forms: Mapped[list["MetaForm"]] = relationship(
        back_populates="table", cascade="all, delete-orphan"
    )


class MetaField(Base):
    __tablename__ = "app_meta_field"
    __table_args__ = (UniqueConstraint("table_id", "phys_name", name="uq_field_per_table"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    phys_name: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    data_type: Mapped[str] = mapped_column(String(20), nullable=False)
    length: Mapped[int | None] = mapped_column(Integer)
    precision: Mapped[int | None] = mapped_column(Integer)
    scale: Mapped[int | None] = mapped_column(Integer)
    nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    default_value: Mapped[str | None] = mapped_column(String(255))
    is_unique: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=0)
    # JSON-encoded list of choices for enum fields
    enum_options: Mapped[str | None] = mapped_column(Text)
    # JSON map {option: chip hue} — designer-chosen status colors (blank = auto hash)
    enum_colors: Mapped[str | None] = mapped_column(Text)
    # for data_type == 'relation' (many-to-one)
    related_table_id: Mapped[int | None] = mapped_column(Integer)
    on_delete: Mapped[str | None] = mapped_column(String(20))
    # validation rules applied in User-mode forms and CSV import
    min_length: Mapped[int | None] = mapped_column(Integer)
    max_length: Mapped[int | None] = mapped_column(Integer)
    min_value: Mapped[str | None] = mapped_column(String(64))
    max_value: Mapped[str | None] = mapped_column(String(64))
    pattern: Mapped[str | None] = mapped_column(String(255))
    # for data_type == 'formula' (computed): the expression + its result kind
    formula: Mapped[str | None] = mapped_column(Text)
    result_type: Mapped[str | None] = mapped_column(String(20))

    table: Mapped["MetaTable"] = relationship(back_populates="fields")


class MetaRelation(Base):
    __tablename__ = "app_meta_relation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(4), nullable=False)  # 'm1' | 'mn'
    from_table_id: Mapped[int] = mapped_column(Integer, nullable=False)
    to_table_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # m1: the FK field on the from-table; mn: NULL
    from_field_id: Mapped[int | None] = mapped_column(Integer)
    # mn: physical junction table name
    junction_phys_name: Mapped[str | None] = mapped_column(String(64))
    on_delete: Mapped[str | None] = mapped_column(String(20))
    # JSON lists of MetaField ids used to label a related record in User-mode
    # pickers. to_* applies to to_table (M:1 + M:N); from_* to from_table (M:N).
    to_display_field_ids: Mapped[str | None] = mapped_column(Text)
    from_display_field_ids: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# UI definitions
# --------------------------------------------------------------------------- #
class MetaForm(Base):
    __tablename__ = "app_meta_form"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(10), nullable=False, default="data")  # data | view
    # service catalog: show this form as a request card on /u/catalog
    in_catalog: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    catalog_group: Mapped[str | None] = mapped_column(String(80))
    # portal: customers may close their own ticket into this status value
    portal_close_state: Mapped[str | None] = mapped_column(String(64))
    # designer-chosen list defaults (query args and saved views still win)
    default_sort: Mapped[str | None] = mapped_column(String(64))     # physical column
    default_order: Mapped[str | None] = mapped_column(String(4))     # asc | desc
    default_per_page: Mapped[int | None] = mapped_column(Integer)

    table: Mapped["MetaTable"] = relationship(back_populates="forms")
    items: Mapped[list["MetaFormField"]] = relationship(
        back_populates="form",
        cascade="all, delete-orphan",
        order_by="MetaFormField.position",
    )


class MetaFormField(Base):
    __tablename__ = "app_meta_form_field"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_form.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), default="field")  # 'field' | 'relation'
    field_id: Mapped[int | None] = mapped_column(Integer)        # kind == 'field'
    relation_id: Mapped[int | None] = mapped_column(Integer)     # kind == 'relation' (m:n)
    # dependent (cascading) picker: filter this relation field by a sibling field's value
    parent_field_id: Mapped[int | None] = mapped_column(Integer)   # controlling field (form table)
    filter_field_id: Mapped[int | None] = mapped_column(Integer)   # match column (target table)
    label_override: Mapped[str | None] = mapped_column(String(160))
    widget: Mapped[str | None] = mapped_column(String(30))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    readonly: Mapped[bool] = mapped_column(Boolean, default=False)
    help_text: Mapped[str | None] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)

    form: Mapped["MetaForm"] = relationship(back_populates="items")


class MetaMenu(Base):
    __tablename__ = "app_meta_menu"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_meta_menu.id", ondelete="CASCADE")
    )
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # group | form | list | dashboard
    target_form_id: Mapped[int | None] = mapped_column(Integer)
    target_table_id: Mapped[int | None] = mapped_column(Integer)
    target_dashboard_id: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)
    icon: Mapped[str | None] = mapped_column(String(40))

    children: Mapped[list["MetaMenu"]] = relationship(
        cascade="all, delete-orphan",
        order_by="MetaMenu.position",
        backref=backref("parent", remote_side=[id]),
    )


# --------------------------------------------------------------------------- #
# Access control & audit
# --------------------------------------------------------------------------- #
ACCESS_NONE, ACCESS_READ, ACCESS_WRITE = "none", "read", "write"
ACCESS_LEVELS = (ACCESS_NONE, ACCESS_READ, ACCESS_WRITE)


class Role(Base):
    """A selectable user role. ``designer`` is the built-in admin (full access)."""
    __tablename__ = "app_role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(60), nullable=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class MetaPermission(Base):
    __tablename__ = "app_meta_permission"
    __table_args__ = (UniqueConstraint("role", "form_id", name="uq_perm_role_form"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    form_id: Mapped[int] = mapped_column(Integer, nullable=False)
    access: Mapped[str] = mapped_column(String(10), nullable=False, default=ACCESS_WRITE)


class Sequence(Base):
    """A per-field counter for auto-number fields."""
    __tablename__ = "app_sequence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    next: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class CompositeUnique(Base):
    """A multi-column UNIQUE constraint on a table."""
    __tablename__ = "app_unique"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    field_ids: Mapped[str] = mapped_column(Text, nullable=False)  # JSON list of MetaField ids


class MetaFieldPermission(Base):
    """Per-role read/write/none access to a single field (default = write)."""
    __tablename__ = "app_field_permission"
    __table_args__ = (UniqueConstraint("role", "field_id", name="uq_fperm_role_field"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    field_id: Mapped[int] = mapped_column(Integer, nullable=False)
    access: Mapped[str] = mapped_column(String(10), nullable=False, default=ACCESS_WRITE)


class AuditLog(Base):
    __tablename__ = "app_audit_log"
    __table_args__ = (Index("ix_audit_table_row", "table_phys", "row_pk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_phys: Mapped[str] = mapped_column(String(64), nullable=False)
    row_pk: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(10), nullable=False)  # create|update|delete|restore
    user_id: Mapped[int | None] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    changes: Mapped[str | None] = mapped_column(Text)


# --------------------------------------------------------------------------- #
# File / image attachments (User mode uploads)
# --------------------------------------------------------------------------- #
class Attachment(Base):
    """One uploaded file belonging to a file/image field of a data row.

    File/image fields are *virtual* (no physical column); their files live here,
    keyed by ``(field_id, row_pk)``. The bytes are on disk (see
    :mod:`app.file_store`); this row holds the metadata.
    """
    __tablename__ = "app_attachment"
    __table_args__ = (Index("ix_attachment_field_row", "field_id", "row_pk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_field.id", ondelete="CASCADE"), nullable=False
    )
    row_pk: Mapped[int] = mapped_column(Integer, nullable=False)
    stored_name: Mapped[str] = mapped_column(String(120), nullable=False)
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[int | None] = mapped_column(Integer)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# --------------------------------------------------------------------------- #
# Per-user saved list views (User mode)
# --------------------------------------------------------------------------- #
class SavedView(Base):
    """A named filter/sort/page-size snapshot of a list view, owned by a user.

    ``query`` holds the list's URL query-string; applying a view simply
    redirects back to the list with it. Per-user UI state — not part of schema
    or data export.
    """
    __tablename__ = "app_saved_view"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    form_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_form.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Workflow(Base):
    """A status workflow attached to an enum field of a table.

    The enum field's choices are the states; this row adds the allowed-transition
    graph (with optional per-transition roles), the initial state, and the
    diagram layout. Part of the app design — included in schema export/import.
    """
    __tablename__ = "app_workflow"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    field_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_field.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    initial_state: Mapped[str | None] = mapped_column(String(64))
    # JSON list of {"from","to","roles":[...]} (roles=[] -> any writer)
    transitions: Mapped[str | None] = mapped_column(Text)
    # JSON {state: {"x","y"}} for the diagram
    layout: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class TriggerRule(Base):
    """A designer rule: when an event happens on a table, run notification actions.

    Fired from :mod:`app.record_service`. ``transitions`` use ``field_id`` +
    optional ``from_state``/``to_state``; an optional condition filters which
    rows match. Each action column is independent (a rule may do several).
    """
    __tablename__ = "app_trigger_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    event: Mapped[str] = mapped_column(String(12), nullable=False)  # create|update|transition|delete
    # transition matching
    field_id: Mapped[int | None] = mapped_column(Integer)
    from_state: Mapped[str | None] = mapped_column(String(64))
    to_state: Mapped[str | None] = mapped_column(String(64))
    # optional condition on the (new) row
    cond_field_id: Mapped[int | None] = mapped_column(Integer)
    cond_op: Mapped[str | None] = mapped_column(String(20))
    cond_value: Mapped[str | None] = mapped_column(String(255))
    # actions
    in_app: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_target: Mapped[str | None] = mapped_column(String(10))  # owner|actor|user
    notify_user_id: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(String(255))
    email_to: Mapped[str | None] = mapped_column(String(255))
    email_subject: Mapped[str | None] = mapped_column(String(255))
    email_body: Mapped[str | None] = mapped_column(Text)
    webhook_url: Mapped[str | None] = mapped_column(String(400))
    set_field_id: Mapped[int | None] = mapped_column(Integer)
    set_value: Mapped[str | None] = mapped_column(String(255))
    # create a record in another table: JSON [{"target", "source"}] with {field} templates
    create_table_id: Mapped[int | None] = mapped_column(Integer)
    create_field_map: Mapped[str | None] = mapped_column(Text)
    # webhook payload shape: json (full event payload) | text ({"text": message} - Slack/Teams)
    webhook_format: Mapped[str | None] = mapped_column(String(10))
    # time-driven firing (event="scheduled"): run actions over matching rows every N minutes
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Notification(Base):
    """A record of one fired trigger action (also the in-app inbox)."""
    __tablename__ = "app_notification"
    __table_args__ = (Index("ix_notif_user", "user_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int | None] = mapped_column(Integer)
    table_phys: Mapped[str | None] = mapped_column(String(64))
    row_pk: Mapped[int | None] = mapped_column(Integer)
    event: Mapped[str | None] = mapped_column(String(12))
    channel: Mapped[str] = mapped_column(String(10), nullable=False)  # in_app|email|webhook|set_field
    user_id: Mapped[int | None] = mapped_column(Integer)             # in-app recipient
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(String(400))         # address / url
    status: Mapped[str] = mapped_column(String(10), nullable=False)  # unread|read|sent|failed|skipped
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ApiToken(Base):
    """A bearer token for the REST API, owned by a user.

    The secret itself is never stored — only its sha256 (``token_hash``). A
    request authenticates by ``Authorization: Bearer <secret>`` and then acts as
    this token's user (see the request_loader in :mod:`app.helpers`).
    """
    __tablename__ = "app_api_token"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Connection(Base):
    """A remote Biggy instance this app can push records into (outbound HTTP).

    Holds the peer's API ``base_url`` and a bearer ``token``. The token must be
    sent on every call, so (unlike [[ApiToken]], which stores only a hash) it is
    kept in clear and treated as write-only in the UI. Deployment config — it is
    exported with the token *redacted* (see :mod:`app.schema_io`).
    """
    __tablename__ = "app_connection"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(400), nullable=False)
    token: Mapped[str | None] = mapped_column(EncryptedText)   # encrypted at rest
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DataSource(Base):
    """Another database whose tables Biggy maps ([[MetaTable]] ``source_id``).

    Holds the connection parts; the engine is built/cached in :mod:`app.db`
    (``engine_for``). The ``password`` is a secret — write-only in the UI and
    redacted from schema export, like the [[Connection]] token. The home database
    (where the ``app_*`` metadata lives) is implicit: ``source_id = NULL``.
    """
    __tablename__ = "app_data_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    driver: Mapped[str] = mapped_column(String(40), default="mysql+pymysql", nullable=False)
    host: Mapped[str | None] = mapped_column(String(255))
    port: Mapped[int | None] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String(120))
    password: Mapped[str | None] = mapped_column(EncryptedText)   # encrypted at rest
    database: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Feed(Base):
    """One chain step: push rows of a local table into a remote table on a peer.

    Fired three ways — on a record event (reusing the [[TriggerRule]] matcher),
    on a schedule (watermark over ``id``), or manually. ``field_map`` is a JSON
    list of ``{"target": <remote col>, "source": <local col or {token}>}``. A
    mapped value to the remote status field drives the *remote* workflow, which
    the peer's API validates. See :mod:`app.feeds` / :mod:`app.connectors`.
    """
    __tablename__ = "app_feed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source_table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("app_connection.id", ondelete="CASCADE"), nullable=False
    )
    target_table: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(10), default="create", nullable=False)  # create|upsert
    match_target_field: Mapped[str | None] = mapped_column(String(64))
    field_map: Mapped[str | None] = mapped_column(Text)  # JSON [{"target","source"}]
    # event matching (mirrors TriggerRule; event '' / 'none' = no live push)
    event: Mapped[str | None] = mapped_column(String(12))
    field_id: Mapped[int | None] = mapped_column(Integer)
    from_state: Mapped[str | None] = mapped_column(String(64))
    to_state: Mapped[str | None] = mapped_column(String(64))
    cond_field_id: Mapped[int | None] = mapped_column(Integer)
    cond_op: Mapped[str | None] = mapped_column(String(20))
    cond_value: Mapped[str | None] = mapped_column(String(255))
    # other firing modes
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    allow_manual: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    skip_api_writes: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    watermark: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Webhook(Base):
    """An inbound endpoint: an external system POSTs JSON, we map it to a record.

    The mirror of [[Feed]] (which pushes *out*). A request authenticates by a
    secret token in the URL (``/hooks/<token>``) — only its sha256 ``token_hash``
    is stored, like [[ApiToken]]. An *optional* ``secret`` additionally requires a
    valid ``X-Biggy-Signature`` HMAC over the raw body. ``field_map`` is a JSON
    list of ``{"target": <local col>, "source": <dotted JSON path>}``; the write
    goes through ``record_service`` as ``user_id`` (so triggers/formulas fire).
    Deployment config — exported with token/secret *redacted* (a fresh token is
    minted on import, see :mod:`app.schema_io`).
    """
    __tablename__ = "app_webhook"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    secret: Mapped[str | None] = mapped_column(EncryptedText)  # optional HMAC shared secret (encrypted at rest)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    mode: Mapped[str] = mapped_column(String(10), default="create", nullable=False)  # create|upsert
    match_field: Mapped[str | None] = mapped_column(String(64))   # target col for upsert
    field_map: Mapped[str | None] = mapped_column(Text)  # JSON [{"target","source"}]
    user_id: Mapped[int | None] = mapped_column(Integer)          # acting/owner user
    # abuse limits — NULL means "use the global WEBHOOK_* config default"
    max_body_bytes: Mapped[int | None] = mapped_column(Integer)   # payload size cap (413)
    rate_limit: Mapped[int | None] = mapped_column(Integer)       # requests/window (0 = off)
    rate_window: Mapped[int | None] = mapped_column(Integer)      # window seconds
    last_received_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class PullSource(Base):
    """A scheduled poll of a remote source that upserts rows into a local table.

    The inbound mirror of [[Feed]] (which pushes *out*): :mod:`app.pull` polls a
    Biggy peer (``kind="peer"`` via a [[Connection]]'s ``/api/v1``) or any REST
    endpoint (``kind="rest"``), maps each remote record by dotted path
    (``field_map``) and upserts it through ``record_service`` (so triggers/feeds/
    formulas fire). Incremental: a ``cursor_field`` over a stored ``watermark``;
    de-duped by ``match_field``. Run by :mod:`app.scheduler`. Exported with
    ``headers`` *redacted* + a clean ``watermark`` (see :mod:`app.schema_io`).
    """
    __tablename__ = "app_pull_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    target_table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(10), default="peer", nullable=False)  # peer|rest
    connection_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_connection.id", ondelete="SET NULL")
    )
    remote_table: Mapped[str | None] = mapped_column(String(64))   # peer: remote table name
    url: Mapped[str | None] = mapped_column(String(400))           # rest: GET URL
    headers: Mapped[str | None] = mapped_column(EncryptedText)     # rest: JSON headers, secret (encrypted at rest)
    records_path: Mapped[str | None] = mapped_column(String(120))  # rest: dotted path to the array
    config: Mapped[str | None] = mapped_column(Text)               # advanced options (JSON; see app.pull)
    auth_secret: Mapped[str | None] = mapped_column(EncryptedText)  # bearer/api-key/basic secret (encrypted at rest)
    field_map: Mapped[str | None] = mapped_column(Text)  # JSON [{"target","source"}]
    mode: Mapped[str] = mapped_column(String(10), default="upsert", nullable=False)  # upsert|create
    match_field: Mapped[str | None] = mapped_column(String(64))    # local col for upsert
    cursor_field: Mapped[str | None] = mapped_column(String(64))   # remote incremental field
    watermark: Mapped[str | None] = mapped_column(String(255))     # last cursor value seen
    page_size: Mapped[int | None] = mapped_column(Integer)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)  # poll cadence (scheduler)
    user_id: Mapped[int | None] = mapped_column(Integer)           # acting user
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_status: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ReportDef(Base):
    """A named group-by/aggregation report, owned by a user.

    ``query`` holds the report builder's URL query-string (group + metrics +
    filters); applying a report opens the report page with it. Per-user UI state
    — not part of schema or data export. Mirrors [[SavedView]].
    """
    __tablename__ = "app_report"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    query: Mapped[str | None] = mapped_column(Text)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # scheduled email digest (run by app.scheduler): recompute + email every N minutes
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    recipients: Mapped[str | None] = mapped_column(String(400))  # comma-separated emails
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class Dashboard(Base):
    """A composable page of [[DashboardWidget]] tiles (charts/KPIs/lists/notes).

    ``owner_user_id`` NULL ⇒ a **shared**, designer-built dashboard (menu-linkable,
    gated by who can read the underlying tables, exported in the schema). A set
    owner ⇒ a **personal** dashboard (per-user state, like [[ReportDef]], not
    exported). Rendered by :mod:`app.dashboards`.
    """
    __tablename__ = "app_dashboard"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    owner_user_id: Mapped[int | None] = mapped_column(Integer)  # NULL = shared
    columns: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    widgets: Mapped[list["DashboardWidget"]] = relationship(
        cascade="all, delete-orphan", order_by="DashboardWidget.position",
        backref="dashboard")


class DashboardWidget(Base):
    """One tile on a [[Dashboard]].

    ``kind`` = ``chart`` / ``number`` / ``list`` / ``text``. For chart & number,
    ``query`` is a report builder query-string (group + metric + filters, parsed by
    :mod:`app.reporting`) over ``table_id``; ``chart_type`` picks bar/line/pie.
    ``content`` holds markdown for a ``text`` tile (or an optional numeric target
    for a ``number`` tile). ``limit`` is the row cap for a ``list`` tile.
    """
    __tablename__ = "app_dashboard_widget"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dashboard_id: Mapped[int] = mapped_column(
        ForeignKey("app_dashboard.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10), default="chart", nullable=False)
    table_id: Mapped[int | None] = mapped_column(Integer)
    query: Mapped[str | None] = mapped_column(Text)
    chart_type: Mapped[str] = mapped_column(String(10), default="bar", nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    limit: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SlaPolicy(Base):
    """A service-level target on a table, enforced by :mod:`app.sla`.

    A per-record clock starts/pauses/stops from the record's ``status_field``
    value (matched against the comma-separated ``*_states`` lists) and measures
    24×7 wall-clock time (paused spans excluded) against ``target_minutes``. The
    live state is written back to ``state_field``/``due_field`` so it shows in
    lists/reports and can drive [[TriggerRule]]s. Breaches are detected by the
    scheduler sweep and escalate via the same action columns as a trigger.
    Mirrors [[TriggerRule]]; per-record state lives in [[SlaClock]].
    """
    __tablename__ = "app_sla_policy"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_id: Mapped[int] = mapped_column(
        ForeignKey("app_meta_table.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    target_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    warn_minutes: Mapped[int | None] = mapped_column(Integer)  # null ⇒ SLA_DEFAULT_WARN_MINUTES
    # clock control: a status (enum) field + comma-separated state lists
    status_field_id: Mapped[int | None] = mapped_column(Integer)
    start_on_create: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    start_states: Mapped[str | None] = mapped_column(String(255))
    pause_states: Mapped[str | None] = mapped_column(String(255))
    stop_states: Mapped[str | None] = mapped_column(String(255))
    # optional applies-when condition on the (new) row
    cond_field_id: Mapped[int | None] = mapped_column(Integer)
    cond_op: Mapped[str | None] = mapped_column(String(20))
    cond_value: Mapped[str | None] = mapped_column(String(255))
    # write-back: keep these record fields updated with live SLA state / deadline
    state_field_id: Mapped[int | None] = mapped_column(Integer)   # enum: on_track|due_soon|paused|met|breached
    due_field_id: Mapped[int | None] = mapped_column(Integer)     # datetime: the deadline
    # breach escalation (same shape as a trigger's actions)
    breach_in_app: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    breach_notify_target: Mapped[str | None] = mapped_column(String(10))  # owner|actor|user
    breach_notify_user_id: Mapped[int | None] = mapped_column(Integer)
    breach_message: Mapped[str | None] = mapped_column(String(255))
    breach_email_to: Mapped[str | None] = mapped_column(String(255))
    breach_email_subject: Mapped[str | None] = mapped_column(String(255))
    breach_email_body: Mapped[str | None] = mapped_column(Text)
    breach_set_field_id: Mapped[int | None] = mapped_column(Integer)
    breach_set_value: Mapped[str | None] = mapped_column(String(255))
    # escalation chain after the breach: JSON list of levels, each
    # {"after_minutes", "notify_target"|"notify_user_id", "email_to", "message"}
    escalations: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class SlaClock(Base):
    """Per-record SLA timing state for one [[SlaPolicy]] (operational, not exported).

    ``state`` is running|paused|met|breached|stopped. While running, ``due_at`` is
    the absolute deadline; on pause it is converted to ``remaining_seconds`` and
    restored (pushed out) on resume — that is how 24×7 pause/resume works.
    """
    __tablename__ = "app_sla_clock"
    __table_args__ = (UniqueConstraint("policy_id", "table_phys", "row_pk",
                                       name="uq_sla_clock"),
                      Index("ix_sla_clock_state", "state"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("app_sla_policy.id", ondelete="CASCADE"), nullable=False
    )
    table_phys: Mapped[str] = mapped_column(String(64), nullable=False)
    row_pk: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(10), default="running", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    due_at: Mapped[datetime | None] = mapped_column(DateTime)
    remaining_seconds: Mapped[int | None] = mapped_column(Integer)
    breached_at: Mapped[datetime | None] = mapped_column(DateTime)
    breach_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    escalation_level: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ApprovalStep(Base):
    """One approver requirement on a workflow transition (run by :mod:`app.approvals`).

    A transition ``from_state → to_state`` of a [[Workflow]] **requires approval** iff
    it has one or more steps. ``position`` orders them: same position = parallel (all
    must approve), different positions run sequentially. Each step is satisfied when an
    eligible approver (``approver_role`` — matched against ``AppUser.role`` — or the
    specific ``approver_user_id``; designers always qualify) approves it. Part of the
    app design — included in schema export/import.
    """
    __tablename__ = "app_approval_step"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(
        ForeignKey("app_workflow.id", ondelete="CASCADE"), nullable=False
    )
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    approver_role: Mapped[str | None] = mapped_column(String(20))
    approver_user_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class ApprovalRequest(Base):
    """A held transition awaiting sign-off (runtime state — not exported).

    Created when a user requests an approval-required transition; the record stays in
    ``from_state`` until every step approves (then [[ApprovalAction]]s drive it to
    ``to_state``) or one rejects. ``current_position`` is the step group being voted on.
    """
    __tablename__ = "app_approval_request"
    __table_args__ = (Index("ix_approval_req_record", "table_phys", "row_pk"),
                      Index("ix_approval_req_state", "state"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workflow_id: Mapped[int] = mapped_column(Integer, nullable=False)
    table_phys: Mapped[str] = mapped_column(String(64), nullable=False)
    row_pk: Mapped[str] = mapped_column(String(255), nullable=False)
    from_state: Mapped[str] = mapped_column(String(64), nullable=False)
    to_state: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    current_position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    requested_by: Mapped[int | None] = mapped_column(Integer)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApprovalAction(Base):
    """One approve/reject decision on an [[ApprovalRequest]] — the sign-off trail."""
    __tablename__ = "app_approval_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("app_approval_request.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(10), nullable=False)  # approve|reject
    comment: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class RateHit(Base):
    """One inbound-webhook request timestamp, for the shared (DB-backed) rate limiter.

    Replaces a per-process in-memory counter so the sliding-window limit is enforced
    across all worker processes. Pure runtime data — never exported; old rows are
    swept opportunistically by the scheduler.
    """
    __tablename__ = "app_rate_hit"
    __table_args__ = (Index("ix_rate_hit_key_at", "key", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AppSetting(Base):
    """One instance-wide setting (branding etc.), editable in Designer mode.

    Key-value so new settings need no migration; blank/missing values fall back
    to the ``Config`` defaults (see :mod:`app.settings`).
    """
    __tablename__ = "app_setting"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    value: Mapped[str | None] = mapped_column(Text)


class Comment(Base):
    """One conversation entry on a data record (staff ⇄ customer).

    ``internal=True`` marks a staff-only work note — never shown in the customer
    portal. Runtime data like audit/notifications: not part of schema export.
    """
    __tablename__ = "app_comment"
    __table_args__ = (Index("ix_comment_record", "table_phys", "row_pk"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_phys: Mapped[str] = mapped_column(String(64), nullable=False)
    row_pk: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[int | None] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    internal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
