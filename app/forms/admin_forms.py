"""Fixed forms for auth, setup, and Designer-mode CRUD.

Select choices that depend on database state are populated in the routes.
"""
from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired
from wtforms import (
    BooleanField,
    IntegerField,
    PasswordField,
    SelectField,
    SelectMultipleField,
    StringField,
    TextAreaField,
)
from wtforms.validators import DataRequired, EqualTo, Length, Optional

from ..helpers import ICON_NAMES
from ..metadata.field_types import SCALAR_TYPES
from ..metadata.models import ROLES
from ..metadata.schema_service import ON_DELETE_CHOICES


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])


class SetupForm(FlaskForm):
    username = StringField("Designer username", validators=[DataRequired(), Length(max=80)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField(
        "Confirm password", validators=[DataRequired(), EqualTo("password")]
    )


class PasswordChangeForm(FlaskForm):
    current = PasswordField("Current password", validators=[DataRequired()])
    new = PasswordField("New password", validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField("Confirm new password",
                            validators=[DataRequired(), EqualTo("new", "Passwords must match.")])


class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=80)])
    password = PasswordField("Password", validators=[Optional(), Length(min=6)])
    role = SelectField("Role", choices=[(r, r) for r in ROLES], validate_choice=False)
    is_active = BooleanField("Active", default=True)
    company_id = SelectField("Company", coerce=int, validate_choice=False,
                             validators=[Optional()])


class MfaCodeForm(FlaskForm):
    code = StringField("Authentication code", validators=[DataRequired(), Length(max=20)])


class TableForm(FlaskForm):
    phys_name = StringField("Table name (identifier)", validators=[DataRequired()])
    label = StringField("Label", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("Description", validators=[Optional()])
    # choices populated in the route (home + data sources)
    source_id = SelectField("Data source", coerce=int, validate_choice=False,
                            validators=[Optional()])
    # primary key: auto-increment 'id' (default) or a custom/natural key column
    pk_mode = SelectField("Primary key", default="auto", validate_choice=False,
                          choices=[("auto", "Auto-increment id (default)"),
                                   ("custom", "Custom key column")])
    pk_name = StringField("Key column name", validators=[Optional(), Length(max=64)])
    pk_type = SelectField("Key type", default="string", validate_choice=False,
                          validators=[Optional()],
                          choices=[("string", "Text"), ("integer", "Integer")])
    pk_length = IntegerField("Key length (text)", validators=[Optional()])


class FieldForm(FlaskForm):
    phys_name = StringField("Column name (identifier)", validators=[DataRequired()])
    label = StringField("Label", validators=[DataRequired(), Length(max=120)])
    data_type = SelectField(
        "Type", choices=[(k, v["label"]) for k, v in SCALAR_TYPES.items()]
    )
    length = IntegerField("Length (text)", validators=[Optional()])
    precision = IntegerField("Precision (decimal)", validators=[Optional()])
    scale = IntegerField("Scale (decimal)", validators=[Optional()])
    enum_options = TextAreaField("Choices (one per line)", validators=[Optional()])
    default_value = StringField("Default value", validators=[Optional()])
    formula = TextAreaField("Formula", validators=[Optional()])
    result_type = SelectField("Formula result", default="number", validate_choice=False,
                              validators=[Optional()],
                              choices=[("number", "Number"), ("text", "Text"),
                                       ("boolean", "Boolean (yes/no)"), ("date", "Date"),
                                       ("datetime", "Date & time")])
    nullable = BooleanField("Nullable", default=True)
    is_unique = BooleanField("Unique", default=False)
    # validation rules
    min_length = IntegerField("Min length (text)", validators=[Optional()])
    max_length = IntegerField("Max length (text)", validators=[Optional()])
    min_value = StringField("Min value (number)", validators=[Optional()])
    max_value = StringField("Max value (number)", validators=[Optional()])
    pattern = StringField("Regex pattern (text)", validators=[Optional()])


class RelationM1Form(FlaskForm):
    name = StringField("Relation name", validators=[DataRequired()])
    from_table_id = SelectField("From table (gets the FK)", coerce=int)
    to_table_id = SelectField("To table (referenced)", coerce=int)
    field_name = StringField("FK column name", validators=[DataRequired()])
    on_delete = SelectField("On delete", choices=[(c, c) for c in ON_DELETE_CHOICES])
    nullable = BooleanField("Nullable", default=True)


class RelationMNForm(FlaskForm):
    name = StringField("Relation name", validators=[DataRequired()])
    from_table_id = SelectField("Table A", coerce=int)
    to_table_id = SelectField("Table B", coerce=int)


class RelationEditForm(FlaskForm):
    """Configure the relation name and which fields label related records."""
    name = StringField("Relation name", validators=[DataRequired()])
    to_display_field_ids = SelectMultipleField(coerce=int, validators=[Optional()])
    from_display_field_ids = SelectMultipleField(coerce=int, validators=[Optional()])


class FormDefForm(FlaskForm):
    name = StringField("Form name (unique)", validators=[DataRequired(), Length(max=120)])
    title = StringField("Title", validators=[DataRequired(), Length(max=160)])
    table_id = SelectField("Data table", coerce=int)
    description = TextAreaField("Description", validators=[Optional()])
    purpose = SelectField("Purpose", default="data", validate_choice=False,
                          validators=[Optional()],
                          choices=[("data", "Data entry (list / add / edit)"),
                                   ("view", "View (read-only record page)")])


class FormItemForm(FlaskForm):
    kind = SelectField("Item type", choices=[("field", "Field"), ("relation", "Many-to-many"),
                                             ("section", "Section heading")])
    field_id = SelectField("Field", coerce=int, validators=[Optional()])
    relation_id = SelectField("Relation (M:N)", coerce=int, validators=[Optional()])
    label_override = StringField("Label override", validators=[Optional()])
    help_text = StringField("Help text", validators=[Optional()])
    required = BooleanField("Required")
    readonly = BooleanField("Read-only")
    position = IntegerField("Position", default=0, validators=[Optional()])


class FormItemEditForm(FlaskForm):
    """Edit the display properties of an existing form item (target is fixed)."""
    label_override = StringField("Label override", validators=[Optional()])
    help_text = StringField("Help text", validators=[Optional()])
    required = BooleanField("Required")
    readonly = BooleanField("Read-only")
    # dependent picker (relation items only); choices set in the route
    parent_field_id = SelectField("Filter by", coerce=int, validators=[Optional()])
    filter_field_id = SelectField("Match on", coerce=int, validators=[Optional()])


class MenuForm(FlaskForm):
    label = StringField("Label", validators=[DataRequired(), Length(max=120)])
    kind = SelectField(
        "Kind", choices=[("group", "Group/heading"), ("form", "Form"), ("list", "List view"),
                         ("dashboard", "Dashboard")]
    )
    parent_id = SelectField("Parent", coerce=int, validators=[Optional()])
    target_form_id = SelectField("Target form", coerce=int, validators=[Optional()])
    target_table_id = SelectField("Target table (list)", coerce=int, validators=[Optional()])
    target_dashboard_id = SelectField("Target dashboard", coerce=int, validators=[Optional()])
    position = IntegerField("Position", default=0, validators=[Optional()])
    icon = SelectField("Icon (optional)", validate_choice=False, validators=[Optional()],
                       choices=[("", "— none —")] + [(n, n) for n in ICON_NAMES])


class DashboardForm(FlaskForm):
    """A composable dashboard (shared or personal). Widgets managed separately."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    description = TextAreaField("Description", validators=[Optional()])
    columns = SelectField("Columns", choices=[("1", "1"), ("2", "2"), ("3", "3")],
                          default="2", validate_choice=False)


class DashboardWidgetForm(FlaskForm):
    """One dashboard tile. Table/query/chart selects populated in the route."""
    title = StringField("Title", validators=[Optional(), Length(max=120)])
    kind = SelectField("Kind", choices=[
        ("chart", "Chart (from a report query)"), ("number", "KPI number"),
        ("list", "List (top-N rows)"), ("text", "Text / markdown")], validate_choice=False)
    table_id = SelectField("Table", coerce=int, validate_choice=False, validators=[Optional()])
    query = StringField("Report query (group/metric/filters)", validators=[Optional()])
    chart_type = SelectField("Chart type", choices=[("bar", "Bar"), ("line", "Line"),
                                                    ("pie", "Pie")], validate_choice=False)
    content = TextAreaField("Text / markdown (or numeric target for a KPI)",
                            validators=[Optional()])
    width = SelectField("Width", choices=[("1", "1 column"), ("2", "2 columns")],
                        default="1", validate_choice=False)
    limit = IntegerField("List row limit", default=5, validators=[Optional()])


class ImportForm(FlaskForm):
    file = FileField("CSV file", validators=[FileRequired()])
    skip_invalid = BooleanField("Import valid rows even if some rows have errors")
    mode = SelectField("Mode", default="insert", validate_choice=False,
                       validators=[Optional()], choices=[
                           ("insert", "Insert new rows only"),
                           ("upsert", "Update existing or insert (upsert)"),
                       ])
    # choices populated in the route (id + unique fields)
    key_column = SelectField("Match existing rows on", validate_choice=False,
                             validators=[Optional()])


class SchemaImportForm(FlaskForm):
    file = FileField("Schema JSON file", validators=[FileRequired()])
    replace_existing = BooleanField("Replace existing model (drops current tables and their data)")


class DataImportForm(FlaskForm):
    file = FileField("Data JSON file", validators=[FileRequired()])
    replace_existing = BooleanField("Replace existing data (clears all rows first)")


class SqlQueryForm(FlaskForm):
    sql = TextAreaField("SQL", validators=[DataRequired()])


_COND_OPS = [("", "— no condition —"), ("eq", "equals"), ("ne", "not equals"),
             ("contains", "contains"), ("not_contains", "does not contain"),
             ("starts_with", "starts with"), ("ends_with", "ends with"),
             ("gt", "greater than"), ("gte", "greater or equal"), ("lt", "less than"),
             ("lte", "less or equal"), ("empty", "is empty"), ("not_empty", "is not empty"),
             ("is_true", "is yes"), ("is_false", "is no")]


class ConnectionForm(FlaskForm):
    """A remote Biggy peer. The token is write-only — blank on edit keeps it."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    base_url = StringField("Base URL (e.g. http://host:5000)",
                           validators=[DataRequired(), Length(max=400)])
    token = StringField("API token (Bearer)", validators=[Optional(), Length(max=255)])
    active = BooleanField("Active", default=True)


class DataSourceForm(FlaskForm):
    """Another database whose tables Biggy maps. Password is write-only on edit."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    driver = StringField("Driver", default="mysql+pymysql",
                         validators=[DataRequired(), Length(max=40)])
    host = StringField("Host", validators=[Optional(), Length(max=255)])
    port = IntegerField("Port", validators=[Optional()])
    username = StringField("Username", validators=[Optional(), Length(max=120)])
    password = PasswordField("Password", validators=[Optional(), Length(max=255)])
    database = StringField("Database", validators=[Optional(), Length(max=120)])
    active = BooleanField("Active", default=True)


class FeedForm(FlaskForm):
    """A feed (push a local table to a remote peer). Selects populated in the route."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    active = BooleanField("Active", default=True)
    connection_id = SelectField("Connection", coerce=int, validate_choice=False)
    target_table = StringField("Target table (remote)",
                               validators=[DataRequired(), Length(max=64)])
    mode = SelectField("Mode", choices=[("create", "Always create a new record"),
                                        ("upsert", "Upsert (match on a key field)")],
                       validate_choice=False)
    match_target_field = StringField("Upsert key — remote field",
                                     validators=[Optional(), Length(max=64)])
    # event matching (mirrors TriggerRuleForm)
    event = SelectField("When", choices=[
        ("", "— manual / scheduled only —"), ("create", "Record created"),
        ("update", "Record updated"), ("transition", "Status transition"),
        ("delete", "Record deleted")], validate_choice=False, validators=[Optional()])
    field_id = SelectField("Status field", coerce=int, validate_choice=False,
                           validators=[Optional()])
    from_state = StringField("From state (blank = any)", validators=[Optional(), Length(max=64)])
    to_state = StringField("To state (blank = any)", validators=[Optional(), Length(max=64)])
    cond_field_id = SelectField("Only if field", coerce=int, validate_choice=False,
                                validators=[Optional()])
    cond_op = SelectField("Operator", choices=_COND_OPS, validate_choice=False,
                          validators=[Optional()])
    cond_value = StringField("Value", validators=[Optional(), Length(max=255)])
    # other firing modes
    schedule_minutes = IntegerField("Run every N minutes (blank = off)",
                                    validators=[Optional()])
    allow_manual = BooleanField("Allow manual 'Send'", default=True)
    skip_api_writes = BooleanField("Skip when the change came via the API (loop guard)",
                                   default=True)


class WebhookForm(FlaskForm):
    """An inbound webhook (receive JSON → a record). Field map built in the route."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    active = BooleanField("Active", default=True)
    mode = SelectField("Mode", choices=[("create", "Always create a new record"),
                                        ("upsert", "Upsert (match on a key field)")],
                       validate_choice=False)
    match_field = StringField("Upsert key — target field(s), comma-separated for composite",
                              validators=[Optional(), Length(max=64)])
    secret = StringField("HMAC secret (optional — blank keeps existing)",
                         validators=[Optional(), Length(max=255)])
    user_id = SelectField("Create records as", coerce=int, validate_choice=False,
                          validators=[Optional()])
    # abuse limits — blank uses the global default
    max_body_bytes = IntegerField("Max payload bytes", validators=[Optional()])
    rate_limit = IntegerField("Rate limit (requests per window; 0 = off)",
                              validators=[Optional()])
    rate_window = IntegerField("Rate window (seconds)", validators=[Optional()])


class PullSourceForm(FlaskForm):
    """A pull source (poll a remote source → upsert locally). Selects set in the route."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    active = BooleanField("Active", default=True)
    kind = SelectField("Source kind", choices=[("peer", "Biggy peer (a Connection)"),
                                               ("rest", "Generic REST endpoint")],
                       validate_choice=False)
    # peer
    connection_id = SelectField("Connection", coerce=int, validate_choice=False,
                                validators=[Optional()])
    remote_table = StringField("Remote table", validators=[Optional(), Length(max=64)])
    # rest
    url = StringField("REST URL (GET)", validators=[Optional(), Length(max=400)])
    headers = TextAreaField("Request headers (JSON — secret, blank keeps existing)",
                            validators=[Optional()])
    records_path = StringField("Records path (dotted; blank = body / data)",
                               validators=[Optional(), Length(max=120)])
    # mapping + incremental
    mode = SelectField("Mode", choices=[("upsert", "Upsert (match on a key field)"),
                                        ("create", "Always create a new record")],
                       validate_choice=False)
    match_field = StringField("Upsert key — local field(s), comma-separated for composite",
                              validators=[Optional(), Length(max=64)])
    cursor_field = StringField("Cursor field (remote; blank = full refresh)",
                               validators=[Optional(), Length(max=64)])
    page_size = IntegerField("Page size", validators=[Optional()])
    schedule_minutes = IntegerField("Poll every N minutes", validators=[Optional()])
    user_id = SelectField("Create records as", coerce=int, validate_choice=False,
                          validators=[Optional()])
    # --- advanced (assembled into the config JSON; a raw escape hatch covers the rest) ---
    auth_type = SelectField("Auth", choices=[
        ("none", "None"), ("bearer", "Bearer token"), ("api_key", "API-key header"),
        ("basic", "Basic auth"), ("query_key", "Query-param key")], validate_choice=False)
    auth_secret = StringField("Auth secret (token/password — blank keeps existing)",
                              validators=[Optional(), Length(max=255)])
    auth_header = StringField("API-key header name", validators=[Optional(), Length(max=64)])
    auth_username = StringField("Basic-auth username", validators=[Optional(), Length(max=120)])
    auth_param = StringField("Query-key param name", validators=[Optional(), Length(max=64)])
    http_method = SelectField("HTTP method", choices=[("GET", "GET"), ("POST", "POST")],
                              validate_choice=False)
    request_body = TextAreaField("Request body (POST; {watermark}/{page} allowed)",
                                 validators=[Optional()])
    pagination_style = SelectField("Pagination", choices=[
        ("none", "Single request"), ("page", "Page number"), ("offset", "Offset / limit"),
        ("cursor", "Cursor token (from response)"), ("link", "Next URL (from response)")],
        validate_choice=False)
    page_param = StringField("Page / offset param", validators=[Optional(), Length(max=64)])
    size_param = StringField("Page-size param", validators=[Optional(), Length(max=64)])
    page_start = IntegerField("Start page / offset", validators=[Optional()])
    next_path = StringField("Next cursor / URL path (dotted)", validators=[Optional(), Length(max=120)])
    max_pages = IntegerField("Max pages", validators=[Optional()])
    cursor_type = SelectField("Cursor type", choices=[
        ("", "auto"), ("number", "number"), ("date", "date"), ("string", "string")],
        validate_choice=False)
    filter_field = StringField("Only import where field (dotted)",
                               validators=[Optional(), Length(max=120)])
    filter_op = SelectField("Operator", choices=_COND_OPS, validate_choice=False,
                            validators=[Optional()])
    filter_value = StringField("Value", validators=[Optional(), Length(max=255)])
    config_raw = TextAreaField("Advanced config (raw JSON: request.params, request.headers, "
                               "transforms…)", validators=[Optional()])


class TriggerRuleForm(FlaskForm):
    """A trigger rule (when → do). Field/user selects are populated in the route."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    active = BooleanField("Active", default=True)
    event = SelectField("When", choices=[
        ("create", "Record created"), ("update", "Record updated"),
        ("transition", "Status transition"), ("delete", "Record deleted"),
        ("scheduled", "Scheduled (time-based)")])
    # scheduled firing (event="scheduled"): run over matching rows every N minutes
    schedule_minutes = IntegerField("Run every N minutes", validators=[Optional()])
    # transition matching
    field_id = SelectField("Status field", coerce=int, validate_choice=False,
                           validators=[Optional()])
    from_state = StringField("From state (blank = any)", validators=[Optional(), Length(max=64)])
    to_state = StringField("To state (blank = any)", validators=[Optional(), Length(max=64)])
    # optional condition
    cond_field_id = SelectField("Only if field", coerce=int, validate_choice=False,
                                validators=[Optional()])
    cond_op = SelectField("Operator", choices=_COND_OPS, validate_choice=False,
                          validators=[Optional()])
    cond_value = StringField("Value", validators=[Optional(), Length(max=255)])
    # actions
    in_app = BooleanField("Send an in-app notification")
    notify_target = SelectField("Notify", choices=[
        ("actor", "The user who made the change"), ("owner", "The record owner"),
        ("user", "A specific user")], validate_choice=False, validators=[Optional()])
    notify_user_id = SelectField("User", coerce=int, validate_choice=False,
                                 validators=[Optional()])
    message = StringField("Message (use {field} placeholders)",
                          validators=[Optional(), Length(max=255)])
    email_to = StringField("Email to (address or {field})", validators=[Optional(), Length(max=255)])
    email_subject = StringField("Email subject", validators=[Optional(), Length(max=255)])
    email_body = TextAreaField("Email body", validators=[Optional()])
    webhook_url = StringField("Webhook URL (POST)", validators=[Optional(), Length(max=400)])
    webhook_format = SelectField("Webhook payload", validate_choice=False,
                                 validators=[Optional()],
                                 choices=[("json", "Full event JSON"),
                                          ("text", 'Message text — {"text": …} (Slack / Teams)')])
    set_field_id = SelectField("Set field", coerce=int, validate_choice=False,
                               validators=[Optional()])
    set_value = StringField("To value (or now / today)", validators=[Optional(), Length(max=255)])
    create_table_id = SelectField("Create a record in", coerce=int, validate_choice=False,
                                  validators=[Optional()])
    create_field_map = TextAreaField(
        "Field map (JSON — [{\"target\": col, \"source\": \"{field} template\"}])",
        validators=[Optional()])


class SlaPolicyForm(FlaskForm):
    """An SLA policy (target + clock control + breach escalation). Selects set in the route."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    active = BooleanField("Active", default=True)
    target_minutes = IntegerField("Target (minutes)", validators=[DataRequired()])
    warn_minutes = IntegerField("Warn when N minutes remain (blank = default)",
                                validators=[Optional()])
    # clock control
    status_field_id = SelectField("Status field (drives the clock)", coerce=int,
                                  validate_choice=False, validators=[Optional()])
    start_on_create = BooleanField("Start the clock when the record is created", default=True)
    start_states = StringField("Running states (comma-separated; blank = any not paused/stopped)",
                               validators=[Optional(), Length(max=255)])
    pause_states = StringField("Paused states (comma-separated)",
                               validators=[Optional(), Length(max=255)])
    stop_states = StringField("Stopped/Done states (comma-separated)",
                              validators=[Optional(), Length(max=255)])
    # applies-when condition
    cond_field_id = SelectField("Only if field", coerce=int, validate_choice=False,
                                validators=[Optional()])
    cond_op = SelectField("Operator", choices=_COND_OPS, validate_choice=False,
                          validators=[Optional()])
    cond_value = StringField("Value", validators=[Optional(), Length(max=255)])
    # write-back
    state_field_id = SelectField("Write SLA state to field", coerce=int,
                                 validate_choice=False, validators=[Optional()])
    due_field_id = SelectField("Write deadline to field", coerce=int,
                               validate_choice=False, validators=[Optional()])
    # breach escalation
    breach_in_app = BooleanField("On breach: send an in-app notification")
    breach_notify_target = SelectField("Notify", choices=[
        ("", "— none —"), ("owner", "The record owner"), ("user", "A specific user")],
        validate_choice=False, validators=[Optional()])
    breach_notify_user_id = SelectField("User", coerce=int, validate_choice=False,
                                        validators=[Optional()])
    breach_message = StringField("Message (use {field} placeholders)",
                                 validators=[Optional(), Length(max=255)])
    breach_email_to = StringField("Email to (address or {field})",
                                  validators=[Optional(), Length(max=255)])
    breach_email_subject = StringField("Email subject", validators=[Optional(), Length(max=255)])
    breach_email_body = TextAreaField("Email body", validators=[Optional()])
    breach_set_field_id = SelectField("Set field", coerce=int, validate_choice=False,
                                      validators=[Optional()])
    breach_set_value = StringField("To value (or now / today)",
                                   validators=[Optional(), Length(max=255)])
    escalations = TextAreaField(
        "Escalation chain (JSON — see help below)", validators=[Optional()])
