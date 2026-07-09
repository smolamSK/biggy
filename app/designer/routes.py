"""Designer mode: define tables, fields, relations, forms and menus.

Each schema change updates the ``app_meta_*`` metadata *and* issues real DDL via
:mod:`app.metadata.schema_service`, so user data lives in genuine MariaDB tables.
"""
import csv
import io
import json
import re
from datetime import datetime
from urllib.parse import urlencode

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask import (
    session as web_session,
)
from flask_login import current_user, login_required
from sqlalchemy import delete, func, or_, select

from .. import (
    adopt,
    connectors,
    data_io,
    examples,
    feeds,
    file_store,
    formula,
    helpers,
    importer,
    pull,
    reconcile,
    reporting,
    scheduler,
    schema_io,
    sql_console,
    workflow,
)
from .. import companies as company_svc
from .. import settings as instance_settings
from ..db import SessionLocal, engine_for, engine_for_table, get_engine, test_source
from ..forms.admin_forms import (
    ConnectionForm,
    DashboardForm,
    DashboardWidgetForm,
    DataImportForm,
    DataSourceForm,
    FeedForm,
    FieldForm,
    FormDefForm,
    FormItemEditForm,
    FormItemForm,
    MenuForm,
    PullSourceForm,
    RelationEditForm,
    RelationM1Form,
    RelationMNForm,
    SchemaImportForm,
    SlaPolicyForm,
    SqlQueryForm,
    TableForm,
    TriggerRuleForm,
    WebhookForm,
)
from ..helpers import CHIP_HUES, designer_required
from ..identifiers import (
    RESERVED_COLUMNS,
    IdentifierError,
    junction_name,
    sanitize_identifier,
    validate_identifier,
)
from ..metadata import schema_service
from ..metadata.field_types import FILE_TYPES, RELATION_TYPE, type_label
from ..metadata.models import (
    ACCESS_LEVELS,
    ACCESS_WRITE,
    ROLE_DESIGNER,
    ROLE_USER,
    ApprovalStep,
    AppUser,
    Attachment,
    AuditLog,
    Company,
    CompositeUnique,
    Connection,
    Dashboard,
    DashboardWidget,
    DataSource,
    Feed,
    MetaField,
    MetaFieldPermission,
    MetaForm,
    MetaFormField,
    MetaMenu,
    MetaPermission,
    MetaRelation,
    MetaTable,
    PullSource,
    ReportDef,
    Role,
    SlaPolicy,
    TriggerRule,
    Webhook,
    Workflow,
)

bp = Blueprint("designer", __name__, url_prefix="/designer")


@bp.before_request
@login_required
@designer_required
def _guard():
    pass


def _s():
    return SessionLocal()


def _tables(session):
    return session.scalars(select(MetaTable).order_by(MetaTable.label)).all()


def _external_readonly(mt):
    """Flash + return True when ``mt`` is an adopted (external) table.

    External tables are mapped, not owned: Biggy must never issue DDL against
    them, so structural-change routes refuse early.
    """
    if mt is not None and not mt.managed:
        flash("This table is external (adopted) — its schema is read-only here.", "warning")
        return True
    return False


def _reorder(session, ordered, item_id, direction):
    """Normalise positions of an ordered list, then swap one item up/down."""
    ordered = list(ordered)
    for i, x in enumerate(ordered):
        x.position = i
    idx = next((i for i, x in enumerate(ordered) if x.id == item_id), None)
    if idx is None:
        return
    swap = idx - 1 if direction == "up" else idx + 1
    if 0 <= swap < len(ordered):
        ordered[idx].position, ordered[swap].position = swap, idx
    session.commit()


def _apply_validation(field, form):
    """Copy validation-rule inputs from a FieldForm onto a MetaField."""
    field.min_length = form.min_length.data
    field.max_length = form.max_length.data
    field.min_value = (form.min_value.data or None)
    field.max_value = (form.max_value.data or None)
    field.pattern = (form.pattern.data or None)


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
@bp.route("/")
def dashboard():
    session = _s()
    return render_template(
        "designer/dashboard.html",
        tables=_tables(session),
        relations=session.scalars(select(MetaRelation)).all(),
        forms=session.scalars(select(MetaForm)).all(),
        menus=session.scalars(select(MetaMenu)).all(),
    )


# --------------------------------------------------------------------------- #
# Instance settings (branding)
# --------------------------------------------------------------------------- #
@bp.route("/settings", methods=["GET", "POST"])
def settings_page():
    session = _s()
    if request.method == "POST":
        accent = "" if request.form.get("accent_default") \
            else (request.form.get("accent") or "").strip()
        if accent and not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
            flash("Accent must be a hex color like #4f46e5.", "danger")
            return redirect(url_for("designer.settings_page"))
        theme = request.form.get("default_theme") or ""
        base_url = (request.form.get("base_url") or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            flash("Base URL must start with http:// or https://.", "danger")
            return redirect(url_for("designer.settings_page"))
        instance_settings.save(session, {
            "app_name": (request.form.get("app_name") or "").strip()[:40],
            "accent": accent,
            "default_theme": theme if theme in instance_settings.THEMES else "",
            "base_url": base_url,
        })
        flash("Settings saved.", "success")
        return redirect(url_for("designer.settings_page"))
    return render_template(
        "designer/settings.html", stored=instance_settings.get_all(session),
        themes=instance_settings.THEMES,
        default_name=current_app.config.get("APP_NAME", "Biggy"))


# --------------------------------------------------------------------------- #
# Companies (tenants) — a tree; access to one implies access to all below
# --------------------------------------------------------------------------- #
def _company_tree(session):
    """(company, depth) rows in tree order; orphans appended at the root level."""
    rows = company_svc.all_companies(session)
    children = {}
    for c in rows:
        children.setdefault(c.parent_id, []).append(c)
    out, seen = [], set()

    def walk(pid, depth):
        for c in sorted(children.get(pid, []), key=lambda x: x.name.lower()):
            if c.id in seen:
                continue
            seen.add(c.id)
            out.append((c, depth))
            walk(c.id, depth + 1)

    walk(None, 0)
    for c in rows:                      # orphaned parents (broken chain) — flat
        if c.id not in seen:
            seen.add(c.id)
            out.append((c, 0))
            walk(c.id, 1)
    return out


@bp.route("/companies", methods=["GET", "POST"])
def companies_home():
    session = _s()
    if request.method == "POST":        # add
        name = (request.form.get("name") or "").strip()[:120]
        raw = request.form.get("parent_id") or ""
        parent_id = int(raw) if raw.isdigit() and int(raw) else None
        if not name:
            flash("Company name is required.", "danger")
        elif session.scalar(select(Company).where(Company.name == name)):
            flash("A company with that name already exists.", "danger")
        else:
            session.add(Company(name=name, parent_id=parent_id))
            session.commit()
            flash("Company added.", "success")
        return redirect(url_for("designer.companies_home"))
    tree = _company_tree(session)
    # per-company: descendants (invalid as its own parent) + assigned user count
    invalid_parents = {c.id: company_svc.subtree_ids(session, c.id) for c, _ in tree}
    user_counts = {}
    for u in session.scalars(select(AppUser)):
        if u.company_id:
            user_counts[u.company_id] = user_counts.get(u.company_id, 0) + 1
    return render_template("designer/companies.html", tree=tree,
                           invalid_parents=invalid_parents, user_counts=user_counts)


@bp.route("/companies/<int:cid>", methods=["POST"])
def company_edit(cid):
    session = _s()
    c = session.get(Company, cid)
    if not c:
        flash("Company not found.", "danger")
        return redirect(url_for("designer.companies_home"))
    name = (request.form.get("name") or "").strip()[:120]
    raw = request.form.get("parent_id") or ""
    parent_id = int(raw) if raw.isdigit() and int(raw) else None
    if not name:
        flash("Company name is required.", "danger")
    elif session.scalar(select(Company).where(Company.name == name, Company.id != cid)):
        flash("A company with that name already exists.", "danger")
    elif parent_id and parent_id in company_svc.subtree_ids(session, cid):
        flash("A company can't be parented under itself or its descendants.", "danger")
    else:
        c.name, c.parent_id = name, parent_id
        session.commit()
        flash("Company updated.", "success")
    return redirect(url_for("designer.companies_home"))


@bp.route("/companies/<int:cid>/delete", methods=["POST"])
def company_delete(cid):
    session = _s()
    c = session.get(Company, cid)
    if not c:
        return redirect(url_for("designer.companies_home"))
    if session.scalar(select(Company.id).where(Company.parent_id == cid).limit(1)):
        flash("Re-parent or delete its child companies first.", "warning")
    elif session.scalar(select(AppUser.id).where(AppUser.company_id == cid).limit(1)):
        flash("Unassign its users first.", "warning")
    else:
        session.delete(c)
        session.commit()
        flash("Company deleted.", "info")
    return redirect(url_for("designer.companies_home"))


# --------------------------------------------------------------------------- #
# Recurring records — create a templated record on a cadence
# --------------------------------------------------------------------------- #
_CADENCES = [(60, "Hourly"), (1440, "Daily"), (10080, "Weekly"), (43200, "Monthly")]


def _parse_value_lines(text_):
    """``column = value`` lines → dict; '#' comments and blanks ignored."""
    out = {}
    for line in (text_ or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"'{line}' — expected: column = value")
        col, val = line.split("=", 1)
        out[col.strip()] = val.strip()
    return out


@bp.route("/recurring", methods=["GET", "POST"])
def recurring_home():
    from ..metadata.models import RecurringRecord
    session = _s()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:120]
        raw = request.form.get("table_id") or ""
        mt = session.get(MetaTable, int(raw)) if raw.isdigit() else None
        cadence = request.form.get("cadence") or ""
        try:
            minutes = int(request.form.get("minutes") or 0) if cadence == "custom" \
                else int(cadence)
        except ValueError:
            minutes = 0
        if not name or mt is None or minutes <= 0:
            flash("A name, a table and a cadence are required.", "danger")
            return redirect(url_for("designer.recurring_home"))
        try:
            values = _parse_value_lines(request.form.get("values"))
            fields = {f.phys_name: f for f in mt.fields}
            for col, val in values.items():
                if col not in fields:
                    raise ValueError(f"'{col}' is not a column of {mt.label}")
                if fields[col].data_type not in ("relation", "user", "company"):
                    importer.coerce_value(fields[col], val)   # fail fast on typos
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("designer.recurring_home"))
        session.add(RecurringRecord(name=name, table_id=mt.id,
                                    field_values=json.dumps(values),
                                    schedule_minutes=minutes))
        session.commit()
        flash("Recurring job scheduled.", "success")
        return redirect(url_for("designer.recurring_home"))

    tmap = {t.id: t for t in _tables(session)}
    rows = [{"j": j, "table": tmap.get(j.table_id),
             "vals": json.loads(j.field_values or "{}")}
            for j in session.scalars(select(RecurringRecord)
                                     .order_by(RecurringRecord.name))]
    return render_template("designer/recurring.html", rows=rows,
                           tables=_tables(session), cadences=_CADENCES)


@bp.route("/recurring/<int:rid>/toggle", methods=["POST"])
def recurring_toggle(rid):
    from ..metadata.models import RecurringRecord
    session = _s()
    j = session.get(RecurringRecord, rid)
    if j:
        j.active = not j.active
        session.commit()
        flash(f"'{j.name}' {'resumed' if j.active else 'paused'}.", "info")
    return redirect(url_for("designer.recurring_home"))


@bp.route("/recurring/<int:rid>/delete", methods=["POST"])
def recurring_delete(rid):
    from ..metadata.models import RecurringRecord
    session = _s()
    j = session.get(RecurringRecord, rid)
    if j:
        session.delete(j)
        session.commit()
        flash("Recurring job removed.", "info")
    return redirect(url_for("designer.recurring_home"))


# --------------------------------------------------------------------------- #
# Maintenance windows — hold SLA/alerts during planned work
# --------------------------------------------------------------------------- #
@bp.route("/maintenance", methods=["GET", "POST"])
def maintenance_home():
    from .. import maintenance
    from ..metadata.models import MaintenanceWindow
    session = _s()
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:120]
        try:
            starts = datetime.fromisoformat(request.form.get("starts_at") or "")
            ends = datetime.fromisoformat(request.form.get("ends_at") or "")
        except ValueError:
            flash("Enter valid start and end times.", "danger")
            return redirect(url_for("designer.maintenance_home"))
        if not name or ends <= starts:
            flash("A name and an end after the start are required.", "danger")
            return redirect(url_for("designer.maintenance_home"))
        raw = request.form.get("table_id") or ""
        table_id = int(raw) if raw.isdigit() and int(raw) else None
        rec_t = request.form.get("record_table_id") or ""
        rec_pk = (request.form.get("record_pk") or "").strip()
        record_table_id = int(rec_t) if rec_t.isdigit() and int(rec_t) else None
        if record_table_id and rec_pk:
            mt = session.get(MetaTable, record_table_id)
            try:
                found = mt is not None and data_service_row_exists(mt, rec_pk)
            except Exception:  # noqa: BLE001
                found = False
            if not found:
                flash("The linked record could not be found.", "danger")
                return redirect(url_for("designer.maintenance_home"))
        else:
            record_table_id, rec_pk = None, None
        session.add(MaintenanceWindow(
            name=name, starts_at=starts, ends_at=ends, table_id=table_id,
            record_table_id=record_table_id, record_pk=rec_pk))
        session.commit()
        flash("Maintenance window scheduled.", "success")
        return redirect(url_for("designer.maintenance_home"))

    windows = session.scalars(select(MaintenanceWindow)
                              .order_by(MaintenanceWindow.starts_at.desc())).all()
    tmap = {t.id: t for t in _tables(session)}
    rows = [{"w": w, "status": maintenance.status(w),
             "table": tmap.get(w.table_id),
             "record_table": tmap.get(w.record_table_id)} for w in windows]
    # optional prefill (the "Plan maintenance" link on a record view)
    prefill = {"record_table_id": request.args.get("rt", ""),
               "record_pk": request.args.get("rp", "")}
    return render_template("designer/maintenance.html", rows=rows,
                           tables=_tables(session), prefill=prefill)


def data_service_row_exists(mt, pk):
    from .. import data_service
    return data_service.get_row(engine_for_table(mt), mt.phys_name, pk) is not None


@bp.route("/maintenance/<int:wid>/delete", methods=["POST"])
def maintenance_delete(wid):
    from ..metadata.models import MaintenanceWindow
    session = _s()
    w = session.get(MaintenanceWindow, wid)
    if w:
        session.delete(w)
        session.commit()
        flash("Maintenance window removed.", "info")
    return redirect(url_for("designer.maintenance_home"))


# --------------------------------------------------------------------------- #
# ER diagram
# --------------------------------------------------------------------------- #
def _diagram_graph(session):
    """Tables (with fields) + relations as a JSON-able graph for the diagram."""
    tables = []
    for t in _tables(session):
        fields = [{"name": "id", "type": "integer", "pk": True, "fk_to": None}]
        for f in t.fields:
            fields.append({
                "name": f.phys_name, "type": type_label(f.data_type), "pk": False,
                "fk_to": f.related_table_id if f.data_type == RELATION_TYPE else None,
            })
        tables.append({
            "id": t.id, "label": t.label, "phys_name": t.phys_name, "fields": fields,
            "url": url_for("designer.table_view", table_id=t.id),
        })

    relations = [
        {"kind": r.kind, "from": r.from_table_id, "to": r.to_table_id, "label": r.name}
        for r in session.scalars(select(MetaRelation).order_by(MetaRelation.id))
    ]
    return {"tables": tables, "relations": relations}


@bp.route("/diagram")
def diagram():
    return render_template("designer/diagram.html", graph=_diagram_graph(_s()))


# --------------------------------------------------------------------------- #
# Adopt existing (external) tables
# --------------------------------------------------------------------------- #
def _source_engine(session, source_id):
    ds = session.get(DataSource, source_id) if source_id else None
    return engine_for(ds)


@bp.route("/adopt")
def adopt_home():
    session = _s()
    source_id = request.args.get("source", type=int) or None
    return render_template(
        "designer/adopt.html", source_id=source_id, sources=_source_choices(session),
        candidates=adopt.list_adoptable(session, _source_engine(session, source_id)))


@bp.route("/adopt", methods=["POST"])
def adopt_run():
    session = _s()
    source_id = request.form.get("source", type=int) or None
    engine = _source_engine(session, source_id)
    names = request.form.getlist("tables")
    with_relations = bool(request.form.get("with_relations"))
    adopted, rels, errors = 0, 0, []
    for name in names:
        mt, err = adopt.adopt_table(session, engine, name, source_id=source_id)
        if mt:
            adopted += 1
        elif err:
            errors.append(f"{name}: {err}")
    if with_relations:
        rels = adopt.adopt_relations(session, engine, source_id=source_id)
    session.commit()
    if adopted:
        msg = f"Adopted {adopted} table(s)" + (f" and {rels} relation(s)" if with_relations else "")
        flash(msg + ".", "success")
    elif not errors:
        flash("No tables selected.", "info")
    for e in errors:
        flash(e, "warning")
    return redirect(url_for("designer.adopt_home", source=source_id))


# --------------------------------------------------------------------------- #
# Tables & fields
# --------------------------------------------------------------------------- #
def _source_choices(session):
    return [(0, "Home database")] + [
        (d.id, d.name) for d in session.scalars(
            select(DataSource).where(DataSource.active.is_(True)).order_by(DataSource.name))]


@bp.route("/tables/new", methods=["GET", "POST"])
def table_new():
    session = _s()
    form = TableForm()
    form.source_id.choices = _source_choices(session)
    if form.validate_on_submit():
        source_id = form.source_id.data or None
        engine = engine_for(session.get(DataSource, source_id) if source_id else None)
        try:
            phys = validate_identifier(form.phys_name.data, kind="Table")
        except IdentifierError as exc:
            flash(str(exc), "danger")
            return render_template("designer/table_form.html", form=form)
        if session.scalar(select(MetaTable).where(MetaTable.phys_name == phys)) \
                or schema_service.table_exists(engine, phys):
            flash(f"A table named '{phys}' already exists.", "danger")
            return render_template("designer/table_form.html", form=form)

        pk_field = None
        pk_col = "id"
        if form.pk_mode.data == "custom":
            try:
                pk_col = validate_identifier(form.pk_name.data or "", kind="Column")
            except IdentifierError as exc:
                flash(f"Primary key: {exc}", "danger")
                return render_template("designer/table_form.html", form=form)
            pk_field = MetaField(
                phys_name=pk_col, label=(form.pk_name.data or pk_col).title(),
                data_type=form.pk_type.data or "string",
                length=form.pk_length.data if form.pk_type.data == "string" else None,
                nullable=False, is_unique=True, position=0)

        mt = MetaTable(phys_name=phys, label=form.label.data,
                       description=form.description.data, source_id=source_id, pk_col=pk_col)
        session.add(mt)
        session.flush()
        if pk_field is not None:
            pk_field.table_id = mt.id
            session.add(pk_field)
        schema_service.create_physical_table(engine, phys, [], pk=pk_field)
        session.commit()
        flash(f"Table '{phys}' created.", "success")
        return redirect(url_for("designer.table_view", table_id=mt.id))
    return render_template("designer/table_form.html", form=form)


@bp.route("/tables/<int:table_id>")
def table_view(table_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if not mt:
        flash("Table not found.", "danger")
        return redirect(url_for("designer.dashboard"))
    relations = session.scalars(
        select(MetaRelation).where(
            or_(MetaRelation.from_table_id == table_id, MetaRelation.to_table_id == table_id)
        )
    ).all()
    by_id = {f.id: f for f in mt.fields}
    uniques = []
    for u in session.scalars(select(CompositeUnique).where(CompositeUnique.table_id == table_id)):
        labels = [by_id[i].label for i in json.loads(u.field_ids or "[]") if i in by_id]
        uniques.append({"u": u, "labels": labels})
    source = session.get(DataSource, mt.source_id) if mt.source_id else None
    return render_template(
        "designer/table_view.html", table=mt, relations=relations, uniques=uniques,
        type_label=type_label, field_form=FieldForm(), source=source,
    )


@bp.route("/tables/<int:table_id>/uniques", methods=["POST"])
def unique_add(table_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if not mt:
        return redirect(url_for("designer.dashboard"))
    if _external_readonly(mt):
        return redirect(url_for("designer.table_view", table_id=table_id))
    engine = engine_for_table(mt)
    ids = [int(x) for x in request.form.getlist("field_ids") if x.isdigit()]
    fields = [f for f in mt.fields if f.id in ids
              and f.data_type not in (RELATION_TYPE, "file", "image", "tags", "json")]
    if len(fields) < 2:
        flash("Pick at least two columns for a composite unique constraint.", "warning")
        return redirect(url_for("designer.table_view", table_id=table_id))
    cols = [f.phys_name for f in fields]
    name = ("uq_" + mt.phys_name + "_" + "_".join(cols))[:64]
    try:
        schema_service.add_composite_unique(engine, mt.phys_name, name, cols)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not add constraint: {exc}", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    session.add(CompositeUnique(table_id=mt.id, name=name,
                                field_ids=json.dumps([f.id for f in fields])))
    session.commit()
    flash("Unique constraint added.", "success")
    return redirect(url_for("designer.table_view", table_id=table_id))


@bp.route("/uniques/<int:uid>/delete", methods=["POST"])
def unique_delete(uid):
    session = _s()
    u = session.get(CompositeUnique, uid)
    table_id = u.table_id if u else None
    if u:
        mt = session.get(MetaTable, u.table_id)
        if _external_readonly(mt):
            return redirect(url_for("designer.table_view", table_id=table_id))
        try:
            schema_service.drop_composite_unique(engine_for_table(mt), mt.phys_name, u.name)
        except Exception:  # noqa: BLE001
            pass
        session.delete(u)
        session.commit()
        flash("Constraint removed.", "info")
    return redirect(url_for("designer.table_view", table_id=table_id) if table_id
                    else url_for("designer.dashboard"))


def _formula_error(session, mt, expr, exclude_field_id=None):
    """Validate a formula against table ``mt``; return an error string or None."""
    cols = {"id"} | {f.phys_name for f in mt.fields
                     if f.data_type not in FILE_TYPES and f.id != exclude_field_id}
    lookup_fields = {f.phys_name for f in mt.fields if f.data_type == RELATION_TYPE}
    rollup_rels = {r.name for r in session.scalars(select(MetaRelation).where(
        or_(MetaRelation.from_table_id == mt.id, MetaRelation.to_table_id == mt.id)))}
    return formula.validate(expr, cols, lookup_fields=lookup_fields, rollup_rels=rollup_rels)


@bp.route("/tables/<int:table_id>/fields", methods=["POST"])
def field_add(table_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if not mt:
        flash("Table not found.", "danger")
        return redirect(url_for("designer.dashboard"))
    if _external_readonly(mt):
        return redirect(url_for("designer.table_view", table_id=table_id))
    engine = engine_for_table(mt)
    form = FieldForm()
    if not form.validate_on_submit():
        flash("Invalid field input.", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    try:
        phys = validate_identifier(form.phys_name.data, kind="Column")
    except IdentifierError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    if phys in RESERVED_COLUMNS or any(f.phys_name == phys for f in mt.fields):
        flash(f"Column '{phys}' is reserved or already exists.", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))

    enum_json = None
    if form.data_type.data in ("enum", "tags"):
        opts = [ln.strip() for ln in (form.enum_options.data or "").splitlines() if ln.strip()]
        if not opts:
            flash("Choice/tags fields need at least one option.", "danger")
            return redirect(url_for("designer.table_view", table_id=table_id))
        enum_json = json.dumps(opts)

    formula_expr = result_type = None
    if form.data_type.data == "formula":
        formula_expr = (form.formula.data or "").strip()
        result_type = form.result_type.data or "number"
        err = _formula_error(session, mt, formula_expr)
        if err:
            flash(f"Formula error: {err}", "danger")
            return redirect(url_for("designer.table_view", table_id=table_id))

    field = MetaField(
        table_id=mt.id, phys_name=phys, label=form.label.data,
        data_type=form.data_type.data, length=form.length.data,
        precision=form.precision.data, scale=form.scale.data,
        nullable=form.nullable.data, is_unique=form.is_unique.data,
        default_value=form.default_value.data or None, enum_options=enum_json,
        formula=formula_expr, result_type=result_type, position=len(mt.fields),
    )
    _apply_validation(field, form)
    session.add(field)
    session.flush()
    if field.data_type not in FILE_TYPES:  # file/image are virtual (no column)
        try:
            schema_service.add_scalar_column(engine, mt.phys_name, field)
        except Exception as exc:  # noqa: BLE001 - surface DDL errors to the designer
            session.rollback()
            flash(f"Could not add column: {exc}", "danger")
            return redirect(url_for("designer.table_view", table_id=table_id))
    if mt.display_field_id is None and form.data_type.data in ("string", "text"):
        mt.display_field_id = field.id
    session.commit()
    if field.data_type == "formula":          # backfill existing rows
        formula.recompute_table(session, engine, mt)
    flash(f"Field '{phys}' added.", "success")
    return redirect(url_for("designer.table_view", table_id=table_id))


@bp.route("/tables/<int:table_id>/fields/<int:field_id>/delete", methods=["POST"])
def field_delete(table_id, field_id):
    session = _s()
    field = session.get(MetaField, field_id)
    mt = session.get(MetaTable, table_id)
    engine = engine_for_table(mt) if mt else get_engine()
    if not field or not mt:
        flash("Field not found.", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    if _external_readonly(mt):
        return redirect(url_for("designer.table_view", table_id=table_id))
    # if this is a relation field, also remove the relation record
    rel = session.scalar(select(MetaRelation).where(MetaRelation.from_field_id == field_id))
    if field.data_type in FILE_TYPES:
        # virtual field: no column to drop; remove its attachments + files
        for att in session.scalars(select(Attachment).where(Attachment.field_id == field_id)):
            file_store.delete(field_id, att.stored_name)
            session.delete(att)
    else:
        try:
            schema_service.drop_column(engine, mt.phys_name, field.phys_name)
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not drop column: {exc}", "danger")
            return redirect(url_for("designer.table_view", table_id=table_id))
    if mt.display_field_id == field.id:
        mt.display_field_id = None
    if rel:
        session.delete(rel)
    session.delete(field)
    session.commit()
    flash("Field deleted.", "info")
    return redirect(url_for("designer.table_view", table_id=table_id))


@bp.route("/tables/<int:table_id>/fields/<int:field_id>/edit", methods=["GET", "POST"])
def field_edit(table_id, field_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    field = session.get(MetaField, field_id)
    if not mt or not field or field.table_id != mt.id:
        flash("Field not found.", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    engine = engine_for_table(mt)
    if _external_readonly(mt):
        return redirect(url_for("designer.table_view", table_id=table_id))
    if field.data_type == RELATION_TYPE:
        flash("Edit relation fields from the Relations page.", "info")
        return redirect(url_for("designer.relations"))

    form = FieldForm(obj=field)
    if request.method == "GET":
        form.data_type.data = field.data_type
        if field.enum_options:
            form.enum_options.data = "\n".join(json.loads(field.enum_options))

    def _render():
        return render_template(
            "designer/field_form.html", form=form, table=mt, field=field,
            saved_options=json.loads(field.enum_options or "[]"),
            saved_colors=json.loads(field.enum_colors or "{}"), hues=CHIP_HUES)

    if form.validate_on_submit():
        try:
            phys = validate_identifier(form.phys_name.data, kind="Column")
        except IdentifierError as exc:
            flash(str(exc), "danger")
            return _render()
        if phys in RESERVED_COLUMNS or any(f.phys_name == phys for f in mt.fields if f.id != field.id):
            flash(f"Column '{phys}' is reserved or already exists.", "danger")
            return _render()
        enum_json = field.enum_options
        if field.data_type in ("enum", "tags"):
            opts = [ln.strip() for ln in (form.enum_options.data or "").splitlines() if ln.strip()]
            if not opts:
                flash("Choice/tags fields need at least one option.", "danger")
                return _render()
            enum_json = json.dumps(opts)
            if field.data_type == "enum":
                # colorval_/colorhue_ pairs from the colors editor; entries for
                # renamed/removed options are dropped, "auto" means hash fallback
                colors = {}
                for j in range(200):                    # generous cap on options
                    val = request.form.get(f"colorval_{j}")
                    if val is None:
                        break
                    hue = request.form.get(f"colorhue_{j}")
                    if val in opts and hue in CHIP_HUES:
                        colors[val] = hue
                field.enum_colors = json.dumps(colors) if colors else None

        if field.data_type == "formula":
            expr = (form.formula.data or "").strip()
            err = _formula_error(session, mt, expr, exclude_field_id=field.id)
            if err:
                flash(f"Formula error: {err}", "danger")
                return _render()
            field.formula = expr
            field.result_type = form.result_type.data or "number"

        old_name = field.phys_name
        field.phys_name = phys
        field.label = form.label.data
        field.length = form.length.data
        field.precision = form.precision.data
        field.scale = form.scale.data
        field.nullable = form.nullable.data
        field.is_unique = form.is_unique.data
        field.default_value = form.default_value.data or None
        field.enum_options = enum_json
        _apply_validation(field, form)  # data_type stays unchanged
        session.flush()
        if field.data_type not in FILE_TYPES:  # virtual fields have no column
            try:
                schema_service.modify_column(engine, mt.phys_name, old_name, field)
            except Exception as exc:  # noqa: BLE001 - surface DDL errors to the designer
                session.rollback()
                flash(f"Could not modify column: {exc}", "danger")
                return _render()
        session.commit()
        if field.data_type == "formula":      # recompute with the new expression
            formula.recompute_table(session, engine, mt)
        flash(f"Field '{phys}' updated.", "success")
        return redirect(url_for("designer.table_view", table_id=table_id))
    return _render()


@bp.route("/tables/<int:table_id>/fields/<int:field_id>/move/<direction>", methods=["POST"])
def field_move(table_id, field_id, direction):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if mt:
        _reorder(session, sorted(mt.fields, key=lambda f: (f.position, f.id)),
                 field_id, direction)
    return redirect(url_for("designer.table_view", table_id=table_id))


@bp.route("/tables/<int:table_id>/display/<int:field_id>", methods=["POST"])
def set_display_field(table_id, field_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if mt:
        mt.display_field_id = field_id
        session.commit()
        flash("Display field updated.", "success")
    return redirect(url_for("designer.table_view", table_id=table_id))


@bp.route("/tables/<int:table_id>/delete", methods=["POST"])
def table_delete(table_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if not mt:
        return redirect(url_for("designer.dashboard"))
    engine = engine_for_table(mt)
    rels = session.scalars(
        select(MetaRelation).where(
            or_(MetaRelation.from_table_id == table_id, MetaRelation.to_table_id == table_id)
        )
    ).all()
    if rels:
        flash("Remove relations involving this table before deleting it.", "warning")
        return redirect(url_for("designer.table_view", table_id=table_id))
    phys, managed = mt.phys_name, mt.managed
    session.delete(mt)  # cascades fields + forms
    session.commit()
    if managed:
        schema_service.drop_physical_table(engine, phys)
        flash(f"Table '{phys}' deleted.", "info")
    else:                       # external: unmap only, never drop the real table
        flash(f"External table '{phys}' unmapped (the real table was kept).", "info")
    return redirect(url_for("designer.dashboard"))


@bp.route("/tables/<int:table_id>/duplicate", methods=["POST"])
def table_duplicate(table_id):
    """Copy a table's structure — scalar fields, uniques, flags. No relations, no data."""
    session = _s()
    src = session.get(MetaTable, table_id)
    if not src:
        flash("Table not found.", "danger")
        return redirect(url_for("designer.dashboard"))
    if _external_readonly(src):
        return redirect(url_for("designer.table_view", table_id=table_id))
    try:
        phys = validate_identifier(request.form.get("phys_name"), kind="Table")
    except IdentifierError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    engine = engine_for_table(src)
    if session.scalar(select(MetaTable).where(MetaTable.phys_name == phys)) \
            or schema_service.table_exists(engine, phys):
        flash(f"A table named '{phys}' already exists.", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))

    mt = MetaTable(phys_name=phys, label=(request.form.get("label") or "").strip()
                   or f"{src.label} (copy)",
                   description=src.description, source_id=src.source_id, pk_col=src.pk_col,
                   track_audit=src.track_audit, soft_delete=src.soft_delete,
                   row_owned=src.row_owned)
    session.add(mt)
    session.flush()

    id_map, columns, pk_field, skipped = {}, [], None, 0
    for f in sorted(src.fields, key=lambda x: (x.position, x.id)):
        if f.data_type == RELATION_TYPE:
            skipped += 1
            continue
        nf = MetaField(
            table_id=mt.id, phys_name=f.phys_name, label=f.label, data_type=f.data_type,
            length=f.length, precision=f.precision, scale=f.scale, nullable=f.nullable,
            default_value=f.default_value, is_unique=f.is_unique, position=f.position,
            enum_options=f.enum_options, min_length=f.min_length, max_length=f.max_length,
            min_value=f.min_value, max_value=f.max_value, pattern=f.pattern,
            formula=f.formula, result_type=f.result_type)
        session.add(nf)
        session.flush()
        id_map[f.id] = nf.id
        if src.pk_col != "id" and f.phys_name == src.pk_col:
            pk_field = nf
        else:
            columns.append(nf)
        if src.display_field_id == f.id:
            mt.display_field_id = nf.id
    try:
        schema_service.create_physical_table(engine, phys, columns, pk=pk_field)
    except Exception as exc:  # noqa: BLE001 - surface DDL errors to the designer
        session.rollback()
        flash(f"Could not create table: {exc}", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    # composite uniques whose columns all made it into the copy
    for u in session.scalars(select(CompositeUnique).where(CompositeUnique.table_id == src.id)):
        old_ids = json.loads(u.field_ids or "[]")
        if not old_ids or not all(i in id_map for i in old_ids):
            continue
        by_id = {f.id: f for f in src.fields}
        cols = [by_id[i].phys_name for i in old_ids]
        name = ("uq_" + phys + "_" + "_".join(cols))[:64]
        try:
            schema_service.add_composite_unique(engine, phys, name, cols)
        except Exception as exc:  # noqa: BLE001
            flash(f"Constraint '{name}' was not copied: {exc}", "warning")
            continue
        session.add(CompositeUnique(table_id=mt.id, name=name,
                                    field_ids=json.dumps([id_map[i] for i in old_ids])))
    session.commit()
    msg = f"Table duplicated as '{phys}' (structure only — no data"
    msg += ", relations not copied)." if skipped else ")."
    flash(msg, "success")
    return redirect(url_for("designer.table_view", table_id=mt.id))


# --------------------------------------------------------------------------- #
# Table from CSV — bootstrap a table (+ data) from a spreadsheet export
# --------------------------------------------------------------------------- #
CSV_TYPE_CHOICES = ("string", "text", "integer", "bigint", "decimal", "float",
                    "boolean", "date", "datetime", "email", "url", "phone")
_CSV_MAX_BYTES = 2 * 1024 * 1024


def _csv_upload_text():
    """The posted CSV, from the file field or the hidden carry-over textarea."""
    f = request.files.get("file")
    if f and f.filename:
        data = f.read(_CSV_MAX_BYTES + 1)
        if len(data) > _CSV_MAX_BYTES:
            raise ValueError("File too large (2 MB max).")
        return data.decode("utf-8-sig", errors="replace")
    text_ = request.form.get("csv_text") or ""
    if len(text_.encode()) > _CSV_MAX_BYTES + 4:
        raise ValueError("File too large (2 MB max).")
    return text_


@bp.route("/tables/from-csv", methods=["GET", "POST"])
def table_from_csv():
    """Two-step wizard: upload → review inferred columns → create table + import."""
    session = _s()
    step = request.form.get("step")
    if request.method != "POST":
        return render_template("designer/table_from_csv.html", step="upload")

    try:
        file_text = _csv_upload_text()
        if not file_text.strip():
            raise ValueError("Choose a CSV file to upload.")
        columns, samples, n_rows = importer.infer_schema(file_text)
    except ValueError as exc:
        flash(str(exc), "danger")
        return render_template("designer/table_from_csv.html", step="upload")

    label = (request.form.get("label") or "").strip()
    if step != "create":
        phys_guess = sanitize_identifier(label or "table1", kind="Table", fallback="table1")
        return render_template(
            "designer/table_from_csv.html", step="review", columns=columns,
            samples=samples, n_rows=n_rows, csv_text=file_text,
            label=label or "Imported data", phys_name=phys_guess,
            type_choices=CSV_TYPE_CHOICES)

    # ---- create: designer-reviewed names/labels/types come back as parallel lists
    def review_ctx(err):
        flash(err, "danger")
        return render_template(
            "designer/table_from_csv.html", step="review", columns=columns,
            samples=samples, n_rows=n_rows, csv_text=file_text,
            label=label or "Imported data",
            phys_name=request.form.get("phys_name", ""), type_choices=CSV_TYPE_CHOICES)

    try:
        phys = validate_identifier(request.form.get("phys_name"), kind="Table")
    except IdentifierError as exc:
        return review_ctx(str(exc))
    engine = get_engine()
    if session.scalar(select(MetaTable).where(MetaTable.phys_name == phys)) \
            or schema_service.table_exists(engine, phys):
        return review_ctx(f"A table named '{phys}' already exists.")

    seen, fields, header_to_col = set(), [], {}
    for i, col in enumerate(columns):
        if not request.form.get(f"include_{i}"):
            continue
        try:
            cname = validate_identifier(request.form.get(f"name_{i}"), kind="Column")
        except IdentifierError as exc:
            return review_ctx(f"Column {i + 1}: {exc}")
        if cname in RESERVED_COLUMNS or cname in seen:
            return review_ctx(f"Column '{cname}' is reserved or duplicated.")
        seen.add(cname)
        dtype = request.form.get(f"type_{i}") or col["data_type"]
        if dtype not in CSV_TYPE_CHOICES:
            dtype = "string"
        fields.append(MetaField(
            phys_name=cname, label=(request.form.get(f"label_{i}") or "").strip() or cname,
            data_type=dtype, length=col.get("length") if dtype == "string" else None,
            precision=12 if dtype == "decimal" else None,
            scale=2 if dtype == "decimal" else None,
            nullable=True, position=len(fields)))
        header_to_col[col["header"]] = cname
    if not fields:
        return review_ctx("Keep at least one column.")

    mt = MetaTable(phys_name=phys, label=label or "Imported data")
    session.add(mt)
    session.flush()
    for f in fields:
        f.table_id = mt.id
        session.add(f)
    session.flush()
    if mt.display_field_id is None:
        first_text = next((f for f in fields if f.data_type in ("string", "text")), None)
        if first_text:
            mt.display_field_id = first_text.id
    try:
        schema_service.create_physical_table(engine, phys, fields)
    except Exception as exc:  # noqa: BLE001 - surface DDL errors to the designer
        session.rollback()
        return review_ctx(f"Could not create table: {exc}")
    session.commit()

    # import the rows: rewrite the header line to the chosen column names, then
    # hand off to the shared importer (headers it doesn't know are ignored)
    body = io.StringIO(file_text)
    original_headers = next(csv.reader(body))
    out = io.StringIO()
    csv.writer(out).writerow(
        [header_to_col.get(h.strip(), f"skipped_{i}") for i, h in enumerate(original_headers)])
    result = importer.import_rows(session, engine, mt, out.getvalue() + body.read(),
                                  skip_invalid=True)
    flash(f"Table '{phys}' created; {result['imported']} of {n_rows} rows imported"
          + (f" ({len(result['errors'])} skipped)." if result["errors"] else "."),
          "success" if result["imported"] or not n_rows else "warning")
    return redirect(url_for("designer.table_view", table_id=mt.id))


# --------------------------------------------------------------------------- #
# Relations
# --------------------------------------------------------------------------- #
@bp.route("/relations")
def relations():
    session = _s()
    tmap = {t.id: t for t in _tables(session)}
    rels = session.scalars(select(MetaRelation)).all()
    return render_template("designer/relations.html", relations=rels, tmap=tmap)


@bp.route("/relations/new-m1", methods=["GET", "POST"])
def relation_new_m1():
    session = _s()
    form = RelationM1Form()
    choices = [(t.id, t.label) for t in _tables(session)]
    form.from_table_id.choices = choices
    form.to_table_id.choices = choices
    if form.validate_on_submit():
        from_t = session.get(MetaTable, form.from_table_id.data)
        to_t = session.get(MetaTable, form.to_table_id.data)
        if from_t.source_id != to_t.source_id:
            flash("Relations can only connect tables in the same data source.", "danger")
            return render_template("designer/relation_form.html", form=form, kind="m1")
        if not from_t.managed:
            flash("Can't add a foreign-key column to an external table.", "danger")
            return render_template("designer/relation_form.html", form=form, kind="m1")
        engine = engine_for_table(from_t)
        try:
            col = validate_identifier(form.field_name.data, kind="Column")
        except IdentifierError as exc:
            flash(str(exc), "danger")
            return render_template("designer/relation_form.html", form=form, kind="m1")
        if col in RESERVED_COLUMNS or any(f.phys_name == col for f in from_t.fields):
            flash(f"Column '{col}' is reserved or already exists on {from_t.phys_name}.", "danger")
            return render_template("designer/relation_form.html", form=form, kind="m1")
        field = MetaField(
            table_id=from_t.id, phys_name=col, label=form.name.data,
            data_type=RELATION_TYPE, related_table_id=to_t.id,
            nullable=form.nullable.data, on_delete=form.on_delete.data,
            position=len(from_t.fields),
        )
        session.add(field)
        session.flush()
        try:
            schema_service.add_relation_column(engine, from_t.phys_name, field, to_t.phys_name)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            flash(f"Could not create relation: {exc}", "danger")
            return render_template("designer/relation_form.html", form=form, kind="m1")
        rel = MetaRelation(
            name=form.name.data, kind="m1", from_table_id=from_t.id,
            to_table_id=to_t.id, from_field_id=field.id, on_delete=form.on_delete.data,
        )
        session.add(rel)
        session.commit()
        flash("Relation created — choose which fields to show in user forms.", "success")
        return redirect(url_for("designer.relation_edit", relation_id=rel.id))
    return render_template("designer/relation_form.html", form=form, kind="m1")


@bp.route("/relations/new-mn", methods=["GET", "POST"])
def relation_new_mn():
    session = _s()
    form = RelationMNForm()
    choices = [(t.id, t.label) for t in _tables(session)]
    form.from_table_id.choices = choices
    form.to_table_id.choices = choices
    if form.validate_on_submit():
        a = session.get(MetaTable, form.from_table_id.data)
        b = session.get(MetaTable, form.to_table_id.data)
        if a.source_id != b.source_id:
            flash("Relations can only connect tables in the same data source.", "danger")
            return render_template("designer/relation_form.html", form=form, kind="mn")
        engine = engine_for_table(a)
        jname = junction_name(a.phys_name, b.phys_name)
        if schema_service.table_exists(engine, jname):
            flash(f"Junction table '{jname}' already exists.", "danger")
            return render_template("designer/relation_form.html", form=form, kind="mn")
        left_col = f"{a.phys_name}_id"
        right_col = f"{b.phys_name}_id"
        if left_col == right_col:
            right_col = f"{b.phys_name}_id_2"
        try:
            schema_service.create_junction_table(
                engine, jname, a.phys_name, left_col, b.phys_name, right_col
            )
        except Exception as exc:  # noqa: BLE001
            flash(f"Could not create junction table: {exc}", "danger")
            return render_template("designer/relation_form.html", form=form, kind="mn")
        rel = MetaRelation(
            name=form.name.data, kind="mn", from_table_id=a.id, to_table_id=b.id,
            junction_phys_name=jname,
        )
        session.add(rel)
        session.commit()
        flash("Relation created — choose which fields to show in user forms.", "success")
        return redirect(url_for("designer.relation_edit", relation_id=rel.id))
    return render_template("designer/relation_form.html", form=form, kind="mn")


@bp.route("/relations/<int:relation_id>/edit", methods=["GET", "POST"])
def relation_edit(relation_id):
    session = _s()
    rel = session.get(MetaRelation, relation_id)
    if not rel:
        flash("Relation not found.", "danger")
        return redirect(url_for("designer.relations"))
    from_t = session.get(MetaTable, rel.from_table_id)
    to_t = session.get(MetaTable, rel.to_table_id)

    form = RelationEditForm()
    form.to_display_field_ids.choices = _field_choices(to_t)
    form.from_display_field_ids.choices = _field_choices(from_t)

    if form.validate_on_submit():
        rel.name = form.name.data
        rel.to_display_field_ids = _ids_json(form.to_display_field_ids.data)
        rel.from_display_field_ids = (
            _ids_json(form.from_display_field_ids.data) if rel.kind == "mn" else None
        )
        session.commit()
        flash("Relation updated.", "success")
        return redirect(url_for("designer.relations"))

    if request.method == "GET":
        form.name.data = rel.name
        form.to_display_field_ids.data = _ids_list(rel.to_display_field_ids)
        form.from_display_field_ids.data = _ids_list(rel.from_display_field_ids)
    return render_template("designer/relation_edit.html", form=form, rel=rel,
                           from_t=from_t, to_t=to_t)


def _field_choices(meta_table):
    if not meta_table:
        return []
    return [(f.id, f"{f.label} ({f.phys_name})") for f in meta_table.fields]


def _ids_json(values):
    ids = [int(v) for v in (values or [])]
    return json.dumps(ids) if ids else None


def _ids_list(json_text):
    try:
        return [int(x) for x in json.loads(json_text)] if json_text else []
    except (ValueError, TypeError):
        return []


@bp.route("/relations/<int:relation_id>/delete", methods=["POST"])
def relation_delete(relation_id):
    session = _s()
    rel = session.get(MetaRelation, relation_id)
    if not rel:
        return redirect(url_for("designer.relations"))
    try:
        if rel.kind == "m1" and rel.from_field_id:
            field = session.get(MetaField, rel.from_field_id)
            if field:
                from_t = session.get(MetaTable, field.table_id)
                if from_t and from_t.managed:    # external FK columns aren't ours to drop
                    schema_service.drop_column(
                        engine_for_table(from_t), from_t.phys_name, field.phys_name)
                session.delete(field)
        elif rel.kind == "mn" and rel.junction_phys_name:
            from_t = session.get(MetaTable, rel.from_table_id)
            schema_service.drop_physical_table(
                engine_for_table(from_t), rel.junction_phys_name)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not drop relation: {exc}", "danger")
        return redirect(url_for("designer.relations"))
    # remove any form items referencing this relation
    for item in session.scalars(
        select(MetaFormField).where(MetaFormField.relation_id == relation_id)
    ).all():
        session.delete(item)
    session.delete(rel)
    session.commit()
    flash("Relation deleted.", "info")
    return redirect(url_for("designer.relations"))


# --------------------------------------------------------------------------- #
# Forms
# --------------------------------------------------------------------------- #
@bp.route("/forms")
def forms():
    session = _s()
    tmap = {t.id: t for t in _tables(session)}
    return render_template(
        "designer/forms.html",
        forms=session.scalars(select(MetaForm).order_by(MetaForm.title)).all(),
        tmap=tmap,
    )


@bp.route("/forms/new", methods=["GET", "POST"])
def form_new():
    session = _s()
    form = FormDefForm()
    form.table_id.choices = [(t.id, t.label) for t in _tables(session)]
    if form.validate_on_submit():
        if session.scalar(select(MetaForm).where(MetaForm.name == form.name.data)):
            flash("A form with that name already exists.", "danger")
        else:
            mf = MetaForm(name=form.name.data, title=form.title.data,
                         table_id=form.table_id.data, description=form.description.data,
                         purpose=(form.purpose.data or "data"))
            session.add(mf)
            session.commit()
            flash("Form created. Add fields below.", "success")
            return redirect(url_for("designer.form_edit", form_id=mf.id))
    return render_template("designer/form_form.html", form=form, title="New form")


@bp.route("/forms/<int:form_id>", methods=["GET", "POST"])
def form_edit(form_id):
    session = _s()
    mf = session.get(MetaForm, form_id)
    if not mf:
        flash("Form not found.", "danger")
        return redirect(url_for("designer.forms"))
    item_form = _build_item_form(session, mf)
    if item_form.validate_on_submit():
        _add_form_item(session, mf, item_form)
        return redirect(url_for("designer.form_edit", form_id=form_id))
    field_map = {f.id: f for f in mf.table.fields}
    rel_map = {r.id: r for r in _mn_relations_for(session, mf.table_id)}
    return render_template(
        "designer/form_edit.html", mf=mf, item_form=item_form,
        field_map=field_map, rel_map=rel_map, type_label=type_label,
    )


@bp.route("/forms/<int:form_id>/catalog", methods=["POST"])
def form_catalog(form_id):
    """Toggle a form's service-catalog card (+ its group)."""
    session = _s()
    mf = session.get(MetaForm, form_id)
    if mf:
        mf.in_catalog = bool(request.form.get("in_catalog"))
        mf.catalog_group = (request.form.get("catalog_group") or "").strip() or None
        session.commit()
        flash("Catalog settings saved.", "success")
    return redirect(url_for("designer.form_edit", form_id=form_id))


def _mn_relations_for(session, table_id):
    return session.scalars(
        select(MetaRelation).where(
            MetaRelation.kind == "mn",
            or_(MetaRelation.from_table_id == table_id,
                MetaRelation.to_table_id == table_id),
        )
    ).all()


def _build_item_form(session, mf):
    form = FormItemForm()
    used = {i.field_id for i in mf.items if i.kind == "field"}
    form.field_id.choices = [(0, "— choose —")] + [
        (f.id, f"{f.label} ({f.phys_name})") for f in mf.table.fields if f.id not in used
    ]
    form.relation_id.choices = [(0, "— choose —")] + [
        (r.id, r.name) for r in _mn_relations_for(session, mf.table_id)
    ]
    return form


def _add_form_item(session, mf, form):
    if form.kind.data == "section":
        if not form.label_override.data:
            flash("Give the section a heading (use the Label field).", "warning")
            return
        item = MetaFormField(
            form_id=mf.id, kind="section",
            label_override=form.label_override.data,
            position=form.position.data or len(mf.items),
        )
    elif form.kind.data == "field":
        if not form.field_id.data:
            flash("Select a field to add.", "warning")
            return
        item = MetaFormField(
            form_id=mf.id, kind="field", field_id=form.field_id.data,
            label_override=form.label_override.data or None,
            help_text=form.help_text.data or None, required=form.required.data,
            readonly=form.readonly.data,
            position=form.position.data or len(mf.items),
        )
    else:
        if not form.relation_id.data:
            flash("Select a relation to add.", "warning")
            return
        item = MetaFormField(
            form_id=mf.id, kind="relation", relation_id=form.relation_id.data,
            label_override=form.label_override.data or None,
            help_text=form.help_text.data or None,
            position=form.position.data or len(mf.items),
        )
    session.add(item)
    session.commit()
    flash("Form item added.", "success")


@bp.route("/forms/<int:form_id>/items/<int:item_id>/delete", methods=["POST"])
def form_item_delete(form_id, item_id):
    session = _s()
    item = session.get(MetaFormField, item_id)
    if item:
        session.delete(item)
        session.commit()
        flash("Item removed.", "info")
    return redirect(url_for("designer.form_edit", form_id=form_id))


@bp.route("/forms/<int:form_id>/items/<int:item_id>/edit", methods=["GET", "POST"])
def form_item_edit(form_id, item_id):
    session = _s()
    item = session.get(MetaFormField, item_id)
    if not item or item.form_id != form_id:
        flash("Item not found.", "danger")
        return redirect(url_for("designer.form_edit", form_id=form_id))
    form = FormItemEditForm(obj=item)

    mf = session.get(MetaField, item.field_id) if (item.kind == "field" and item.field_id) else None
    is_relation = bool(mf and mf.data_type == RELATION_TYPE)
    if is_relation:
        target = session.get(MetaTable, mf.related_table_id)
        form.parent_field_id.choices = [(0, "— none —")] + [
            (f.id, f"{f.label} ({f.phys_name})") for f in item.form.table.fields
            if f.data_type == RELATION_TYPE and f.id != mf.id]
        form.filter_field_id.choices = [(0, "— auto —")] + [
            (f.id, f"{f.label} ({f.phys_name})") for f in (target.fields if target else [])
            if f.data_type == RELATION_TYPE]
    else:
        form.parent_field_id.choices = [(0, "—")]
        form.filter_field_id.choices = [(0, "—")]

    if form.validate_on_submit():
        item.label_override = form.label_override.data or None
        item.help_text = form.help_text.data or None
        item.required = form.required.data
        item.readonly = form.readonly.data
        if is_relation:
            item.parent_field_id = form.parent_field_id.data or None
            item.filter_field_id = form.filter_field_id.data or None
        session.commit()
        flash("Item updated.", "success")
        return redirect(url_for("designer.form_edit", form_id=form_id))

    if request.method == "GET":
        form.parent_field_id.data = item.parent_field_id or 0
        form.filter_field_id.data = item.filter_field_id or 0
    return render_template("designer/form_item_form.html", form=form, mf=item.form, item=item,
                           is_relation=is_relation)


@bp.route("/forms/<int:form_id>/items/<int:item_id>/move/<direction>", methods=["POST"])
def form_item_move(form_id, item_id, direction):
    session = _s()
    mf = session.get(MetaForm, form_id)
    if mf:
        _reorder(session, sorted(mf.items, key=lambda i: (i.position, i.id)), item_id, direction)
    return redirect(url_for("designer.form_edit", form_id=form_id))


@bp.route("/forms/<int:form_id>/delete", methods=["POST"])
def form_delete(form_id):
    session = _s()
    mf = session.get(MetaForm, form_id)
    if mf:
        session.delete(mf)
        session.commit()
        flash("Form deleted.", "info")
    return redirect(url_for("designer.forms"))


@bp.route("/forms/<int:form_id>/purpose", methods=["POST"])
def form_purpose(form_id):
    session = _s()
    mf = session.get(MetaForm, form_id)
    if mf and request.form.get("purpose") in ("data", "view"):
        mf.purpose = request.form["purpose"]
        session.commit()
        flash("Form purpose updated.", "success")
    return redirect(url_for("designer.form_edit", form_id=form_id))


# --------------------------------------------------------------------------- #
# Form scaffolding: generate from table / add-all / duplicate
# --------------------------------------------------------------------------- #
def _generate_form_items(session, mf):
    """Add an item for every table field and m:n relation not yet on the form.

    Fields keep their table order; returns the number of items added.
    """
    used_fields = {i.field_id for i in mf.items if i.kind == "field"}
    used_rels = {i.relation_id for i in mf.items if i.kind == "relation"}
    pos, added = len(mf.items), 0
    for f in mf.table.fields:
        if f.id in used_fields:
            continue
        session.add(MetaFormField(form_id=mf.id, kind="field", field_id=f.id, position=pos))
        pos, added = pos + 1, added + 1
    for r in _mn_relations_for(session, mf.table_id):
        if r.id in used_rels:
            continue
        session.add(MetaFormField(form_id=mf.id, kind="relation", relation_id=r.id, position=pos))
        pos, added = pos + 1, added + 1
    return added


def _unique_form_name(session, base):
    name, n = base, 2
    while session.scalar(select(MetaForm).where(MetaForm.name == name)):
        name, n = f"{base}_{n}", n + 1
    return name


@bp.route("/tables/<int:table_id>/generate-form", methods=["POST"])
def generate_form(table_id):
    """One-click scaffolding: a data form with every field and m:n relation,
    optionally a read-only view form and a User-mode menu entry too."""
    session = _s()
    mt = session.get(MetaTable, table_id)
    if not mt:
        flash("Table not found.", "danger")
        return redirect(url_for("designer.dashboard"))
    mf = MetaForm(name=_unique_form_name(session, f"{mt.phys_name}_form"),
                  title=mt.label, table_id=mt.id)
    session.add(mf)
    session.flush()
    n = _generate_form_items(session, mf)
    made = [f"a form with {n} item(s)"]
    if request.form.get("with_view"):
        vf = MetaForm(name=_unique_form_name(session, f"{mt.phys_name}_view"),
                      title=mt.label, table_id=mt.id, purpose="view")
        session.add(vf)
        session.flush()
        _generate_form_items(session, vf)
        made.append("a view form")
    if request.form.get("with_menu"):
        pos = session.scalar(select(func.max(MetaMenu.position))
                             .where(MetaMenu.parent_id.is_(None))) or 0
        session.add(MetaMenu(label=mt.label, kind="form", target_form_id=mf.id,
                             position=pos + 1))
        made.append("a menu entry")
    session.commit()
    flash("Generated " + ", ".join(made) + ".", "success")
    return redirect(url_for("designer.form_edit", form_id=mf.id))


@bp.route("/forms/<int:form_id>/add-all", methods=["POST"])
def form_add_all(form_id):
    session = _s()
    mf = session.get(MetaForm, form_id)
    if not mf:
        flash("Form not found.", "danger")
        return redirect(url_for("designer.forms"))
    n = _generate_form_items(session, mf)
    session.commit()
    if n:
        flash(f"Added {n} missing item(s).", "success")
    else:
        flash("Nothing to add — every field is already on the form.", "info")
    return redirect(url_for("designer.form_edit", form_id=form_id))


@bp.route("/forms/<int:form_id>/defaults", methods=["POST"])
def form_defaults(form_id):
    """Designer-chosen list defaults (sort/direction/page size)."""
    session = _s()
    mf = session.get(MetaForm, form_id)
    if not mf:
        flash("Form not found.", "danger")
        return redirect(url_for("designer.forms"))
    sort = request.form.get("default_sort") or None
    valid = {f.phys_name for f in mf.table.fields} | {mf.table.pk_col}
    mf.default_sort = sort if sort in valid else None
    order = request.form.get("default_order")
    mf.default_order = order if order in ("asc", "desc") else None
    try:
        pp = int(request.form.get("default_per_page") or 0)
    except (TypeError, ValueError):
        pp = 0
    mf.default_per_page = pp or None
    session.commit()
    flash("List defaults saved.", "success")
    return redirect(url_for("designer.form_edit", form_id=form_id))


@bp.route("/forms/<int:form_id>/duplicate", methods=["POST"])
def form_duplicate(form_id):
    session = _s()
    src = session.get(MetaForm, form_id)
    if not src:
        flash("Form not found.", "danger")
        return redirect(url_for("designer.forms"))
    mf = MetaForm(name=_unique_form_name(session, f"{src.name}_copy"),
                  title=f"{src.title} (copy)", table_id=src.table_id,
                  description=src.description, purpose=src.purpose,
                  in_catalog=src.in_catalog, catalog_group=src.catalog_group)
    session.add(mf)
    session.flush()
    for it in sorted(src.items, key=lambda i: (i.position, i.id)):
        session.add(MetaFormField(
            form_id=mf.id, kind=it.kind, field_id=it.field_id, relation_id=it.relation_id,
            parent_field_id=it.parent_field_id, filter_field_id=it.filter_field_id,
            label_override=it.label_override, widget=it.widget, required=it.required,
            readonly=it.readonly, help_text=it.help_text, position=it.position))
    session.commit()
    flash(f"Form duplicated as '{mf.title}'.", "success")
    return redirect(url_for("designer.form_edit", form_id=mf.id))


@bp.route("/catalog", methods=["GET", "POST"])
def catalog_home():
    """Central service-catalog editor: every data form's card in one table.

    Writes the same MetaForm columns as the per-form panel (form_catalog); the
    user-mode Catalog/My requests links appear once anything is flagged.
    """
    session = _s()
    forms = session.scalars(select(MetaForm).where(MetaForm.purpose != "view")
                            .order_by(MetaForm.title)).all()
    # per form: the status (first enum) field's options — for the close-state select
    status_opts = {}
    for f in forms:
        sf = next((fd for fd in f.table.fields if fd.data_type == "enum"), None)
        status_opts[f.id] = json.loads(sf.enum_options or "[]") if sf else []
    if request.method == "POST":
        for f in forms:
            f.in_catalog = bool(request.form.get(f"in_{f.id}"))
            f.catalog_group = (request.form.get(f"group_{f.id}") or "").strip() or None
            f.description = (request.form.get(f"desc_{f.id}") or "").strip() or None
            close = request.form.get(f"close_{f.id}") or None
            f.portal_close_state = close if close in status_opts[f.id] else None
        session.commit()
        flash("Catalog saved.", "success")
        return redirect(url_for("designer.catalog_home"))
    # forms whose submissions can't show under "My requests" (no owner stamps)
    unstamped = {f.id for f in forms
                 if not (f.table.track_audit or f.table.row_owned)}
    return render_template("designer/catalog.html", forms=forms, unstamped=unstamped,
                           status_opts=status_opts)


# --------------------------------------------------------------------------- #
# Menus
# --------------------------------------------------------------------------- #
@bp.route("/menus")
def menus():
    session = _s()
    roots = session.scalars(
        select(MetaMenu).where(MetaMenu.parent_id.is_(None)).order_by(MetaMenu.position)
    ).all()
    return render_template("designer/menus.html", roots=roots)


@bp.route("/menus/new", methods=["GET", "POST"])
def menu_new():
    return _menu_form(None)


@bp.route("/menus/<int:menu_id>/edit", methods=["GET", "POST"])
def menu_edit(menu_id):
    return _menu_form(menu_id)


def _menu_form(menu_id):
    session = _s()
    item = session.get(MetaMenu, menu_id) if menu_id else None
    form = MenuForm(obj=item)
    groups = session.scalars(
        select(MetaMenu).where(MetaMenu.kind == "group").order_by(MetaMenu.label)
    ).all()
    form.parent_id.choices = [(0, "— top level —")] + [
        (g.id, g.label) for g in groups if g.id != menu_id
    ]
    form.target_form_id.choices = [(0, "—")] + [
        (f.id, f.title) for f in session.scalars(select(MetaForm)).all()
    ]
    form.target_table_id.choices = [(0, "—")] + [(t.id, t.label) for t in _tables(session)]
    form.target_dashboard_id.choices = [(0, "—")] + [
        (d.id, d.name) for d in session.scalars(
            select(Dashboard).where(Dashboard.owner_user_id.is_(None)).order_by(Dashboard.name))]
    if request.method == "GET" and item is not None:
        form.target_dashboard_id.data = item.target_dashboard_id or 0

    if form.validate_on_submit():
        if item is None:
            item = MetaMenu()
            session.add(item)
        item.label = form.label.data
        item.kind = form.kind.data
        item.parent_id = form.parent_id.data or None
        item.target_form_id = form.target_form_id.data or None
        item.target_table_id = form.target_table_id.data or None
        item.target_dashboard_id = form.target_dashboard_id.data or None
        item.position = form.position.data or 0
        item.icon = form.icon.data or None
        session.commit()
        flash("Menu saved.", "success")
        return redirect(url_for("designer.menus"))
    return render_template("designer/menu_form.html", form=form,
                           title="Edit menu" if item else "New menu")


@bp.route("/menus/<int:menu_id>/move/<direction>", methods=["POST"])
def menu_move(menu_id, direction):
    session = _s()
    item = session.get(MetaMenu, menu_id)
    if item:
        cond = (MetaMenu.parent_id.is_(None) if item.parent_id is None
                else MetaMenu.parent_id == item.parent_id)
        siblings = session.scalars(
            select(MetaMenu).where(cond).order_by(MetaMenu.position, MetaMenu.id)
        ).all()
        _reorder(session, siblings, menu_id, direction)
    return redirect(url_for("designer.menus"))


@bp.route("/menus/<int:menu_id>/delete", methods=["POST"])
def menu_delete(menu_id):
    session = _s()
    item = session.get(MetaMenu, menu_id)
    if item:
        session.delete(item)
        session.commit()
        flash("Menu item deleted.", "info")
    return redirect(url_for("designer.menus"))


# --------------------------------------------------------------------------- #
# Schema export / import
# --------------------------------------------------------------------------- #
def _model_stats(session):
    count = lambda model: session.scalar(select(func.count()).select_from(model))  # noqa: E731
    return {"tables": count(MetaTable), "relations": count(MetaRelation),
            "forms": count(MetaForm), "menus": count(MetaMenu)}


def _backup_page(session, *, form=None, data_form=None, result=None, data_result=None):
    return render_template(
        "designer/schema.html", stats=_model_stats(session),
        form=form or SchemaImportForm(), data_form=data_form or DataImportForm(),
        result=result, data_result=data_result,
    )


@bp.route("/schema")
def schema_home():
    return _backup_page(_s())


@bp.route("/schema/export.json")
def schema_export():
    payload = json.dumps(schema_io.export_schema(_s()), indent=2, default=str)
    return Response(
        payload, mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=biggy-schema.json"},
    )


@bp.route("/schema/import", methods=["POST"])
def schema_import():
    session = _s()
    form = SchemaImportForm()
    result = None
    if form.validate_on_submit():
        try:
            data = json.loads(form.file.data.read().decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            flash("Could not read the file as JSON.", "danger")
        else:
            try:
                result = schema_io.import_schema(
                    session, get_engine(), data, replace=form.replace_existing.data
                )
                flash(
                    "Imported {tables} table(s), {relations} relation(s), "
                    "{forms} form(s), {menus} menu item(s).".format(**result),
                    "success",
                )
            except schema_io.SchemaError as exc:
                flash(str(exc), "warning")
            except Exception as exc:  # noqa: BLE001 - surface DDL/import errors
                flash(f"Import failed: {exc}", "danger")
    return _backup_page(session, form=form, result=result)


@bp.route("/data/export.json")
def data_export():
    payload = json.dumps(data_io.export_data(_s(), get_engine()), indent=2, default=str)
    return Response(
        payload, mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=biggy-data.json"},
    )


@bp.route("/data/import", methods=["POST"])
def data_import():
    session = _s()
    form = DataImportForm()
    data_result = None
    if form.validate_on_submit():
        try:
            data = json.loads(form.file.data.read().decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError, AttributeError):
            flash("Could not read the file as JSON.", "danger")
        else:
            try:
                data_result = data_io.import_data(
                    session, get_engine(), data, replace=form.replace_existing.data
                )
                msg = "Restored {rows} row(s) into {tables} table(s).".format(**data_result)
                if data_result["skipped"]:
                    msg += " Skipped unknown tables: " + ", ".join(data_result["skipped"]) + "."
                flash(msg, "success")
            except data_io.DataError as exc:
                flash(str(exc), "warning")
            except Exception as exc:  # noqa: BLE001 - surface import errors
                flash(f"Data import failed: {exc}", "danger")
    return _backup_page(session, data_form=form, data_result=data_result)


# --------------------------------------------------------------------------- #
# Example / demo models
# --------------------------------------------------------------------------- #
@bp.route("/examples")
def examples_home():
    from .. import itsm_modules
    return render_template("designer/examples.html", examples=examples.EXAMPLES,
                           modules=itsm_modules.MODULES,
                           module_status=itsm_modules.status(_s()))


@bp.route("/modules/<key>/enable", methods=["POST"])
def module_enable(key):
    from .. import itsm_modules
    if key not in itsm_modules.MODULES:
        flash("Unknown module.", "danger")
        return redirect(url_for("designer.examples_home"))
    session = _s()
    try:
        added = itsm_modules.enable(session, key)
        flash(f"'{itsm_modules.MODULES[key]['title']}' enabled — its forms are in "
              "the ITSM menu group." if added else "That module is already enabled.",
              "success" if added else "info")
    except Exception as exc:  # noqa: BLE001 - surface import errors to the designer
        session.rollback()
        flash(f"Could not enable the module: {exc}", "danger")
    return redirect(url_for("designer.examples_home"))


@bp.route("/examples/<key>/load", methods=["POST"])
def example_load(key):
    ex = examples.EXAMPLES.get(key)
    if not ex:
        flash("Unknown example.", "danger")
        return redirect(url_for("designer.examples_home"))
    session = _s()
    engine = get_engine()
    schema, data = ex["build"]()
    try:
        schema_io.import_schema(session, engine, schema, replace=True)
        result = data_io.import_data(session, engine, data, replace=True)
        flash(f"Loaded the '{ex['title']}' example "
              f"({result['rows']} sample row(s)).", "success")
    except Exception as exc:  # noqa: BLE001 - surface import errors
        flash(f"Could not load example: {exc}", "danger")
    return redirect(url_for("designer.examples_home"))


# --------------------------------------------------------------------------- #
# Per-table behaviour flags, permissions, audit log
# --------------------------------------------------------------------------- #
@bp.route("/tables/<int:table_id>/flags", methods=["POST"])
def table_flags(table_id):
    session = _s()
    mt = session.get(MetaTable, table_id)
    if not mt:
        return redirect(url_for("designer.dashboard"))
    engine = engine_for_table(mt)
    mt.track_audit = bool(request.form.get("track_audit"))
    mt.soft_delete = bool(request.form.get("soft_delete"))
    mt.row_owned = bool(request.form.get("row_owned"))
    session.flush()
    try:
        schema_service.ensure_record_columns(engine, mt)
    except Exception as exc:  # noqa: BLE001 - surface DDL errors
        session.rollback()
        flash(f"Could not update table options: {exc}", "danger")
        return redirect(url_for("designer.table_view", table_id=table_id))
    session.commit()
    flash("Table options updated.", "success")
    return redirect(url_for("designer.table_view", table_id=table_id))


@bp.route("/permissions", methods=["GET", "POST"])
def permissions():
    session = _s()
    helpers.ensure_roles(session)
    forms = session.scalars(select(MetaForm).order_by(MetaForm.title)).all()
    roles = [r for r in session.scalars(select(Role).order_by(Role.name))
             if r.name != ROLE_DESIGNER]
    existing = {(p.role, p.form_id): p for p in session.scalars(select(MetaPermission))}
    if request.method == "POST":
        for f in forms:
            for role in roles:
                val = request.form.get(f"access_{role.name}_{f.id}")
                if val is None and role.name == ROLE_USER:  # legacy field name → user role
                    val = request.form.get(f"access_{f.id}")
                if val not in ACCESS_LEVELS:
                    val = ACCESS_WRITE
                key = (role.name, f.id)
                if key in existing:
                    existing[key].access = val
                else:
                    session.add(MetaPermission(role=role.name, form_id=f.id, access=val))
        session.commit()
        flash("Permissions saved.", "success")
        return redirect(url_for("designer.permissions"))
    rows = [{"form": f, "access": {role.name: (existing[(role.name, f.id)].access
                                               if (role.name, f.id) in existing else ACCESS_WRITE)
                                   for role in roles}} for f in forms]
    return render_template("designer/permissions.html", rows=rows, roles=roles, levels=ACCESS_LEVELS)


@bp.route("/roles", methods=["GET", "POST"])
def roles():
    session = _s()
    helpers.ensure_roles(session)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip().lower()
        label = (request.form.get("label") or "").strip() or name.title()
        if not name or not name.replace("_", "").isalnum():
            flash("Role name must be a simple identifier (letters/digits/underscore).", "warning")
        elif session.scalar(select(Role).where(Role.name == name)):
            flash("That role already exists.", "info")
        else:
            session.add(Role(name=name, label=label))
            session.commit()
            flash(f"Role '{name}' created.", "success")
        return redirect(url_for("designer.roles"))
    items = session.scalars(select(Role).order_by(Role.name)).all()
    counts = {r.name: session.scalar(select(func.count()).select_from(AppUser)
                                     .where(AppUser.role == r.name)) for r in items}
    return render_template("designer/roles.html", roles=items, counts=counts)


@bp.route("/tables/<int:table_id>/field-permissions", methods=["GET", "POST"])
def field_permissions(table_id):
    session = _s()
    helpers.ensure_roles(session)
    mt = session.get(MetaTable, table_id)
    if not mt:
        flash("Table not found.", "danger")
        return redirect(url_for("designer.dashboard"))
    roles = [r for r in session.scalars(select(Role).order_by(Role.name))
             if r.name != ROLE_DESIGNER]
    field_ids = [f.id for f in mt.fields]
    existing = {(p.role, p.field_id): p for p in session.scalars(
        select(MetaFieldPermission).where(MetaFieldPermission.field_id.in_(field_ids)))}
    if request.method == "POST":
        for f in mt.fields:
            for role in roles:
                val = request.form.get(f"facc_{role.name}_{f.id}", ACCESS_WRITE)
                if val not in ACCESS_LEVELS:
                    val = ACCESS_WRITE
                key = (role.name, f.id)
                if key in existing:
                    existing[key].access = val
                else:
                    session.add(MetaFieldPermission(role=role.name, field_id=f.id, access=val))
        session.commit()
        flash("Field permissions saved.", "success")
        return redirect(url_for("designer.field_permissions", table_id=table_id))
    rows = [{"field": f, "access": {role.name: (existing[(role.name, f.id)].access
                                               if (role.name, f.id) in existing else ACCESS_WRITE)
                                    for role in roles}} for f in mt.fields]
    return render_template("designer/field_permissions.html", table=mt, rows=rows,
                           roles=roles, levels=ACCESS_LEVELS)


@bp.route("/roles/<int:role_id>/delete", methods=["POST"])
def role_delete(role_id):
    session = _s()
    r = session.get(Role, role_id)
    if r and not r.builtin:
        if session.scalar(select(func.count()).select_from(AppUser).where(AppUser.role == r.name)):
            flash("That role is assigned to users — reassign them first.", "warning")
        else:
            session.execute(delete(MetaPermission).where(MetaPermission.role == r.name))
            session.execute(delete(MetaFieldPermission).where(MetaFieldPermission.role == r.name))
            session.delete(r)
            session.commit()
            flash("Role deleted.", "info")
    return redirect(url_for("designer.roles"))


@bp.route("/audit")
def audit_log():
    session = _s()
    table = request.args.get("table")
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(200)
    if table:
        stmt = stmt.where(AuditLog.table_phys == table)
    logs = session.scalars(stmt).all()
    names = {u.id: u.username for u in session.scalars(select(AppUser))}
    tids = {t.phys_name: t.id for t in session.scalars(select(MetaTable))}
    entries = [{
        "table": lg.table_phys, "table_id": tids.get(lg.table_phys),
        "row_pk": lg.row_pk, "action": lg.action,
        "user": names.get(lg.user_id, "—"), "at": lg.at,
        "changes": json.loads(lg.changes) if lg.changes else None,
    } for lg in logs]
    audited = [t.phys_name for t in _tables(session) if t.track_audit]
    return render_template("designer/audit.html", entries=entries, tables=audited, table=table)


# --------------------------------------------------------------------------- #
# Read-only SQL console
# --------------------------------------------------------------------------- #
@bp.route("/query", methods=["GET", "POST"])
def query():
    session = _s()
    form = SqlQueryForm()
    tables = [t.phys_name for t in _tables(session)]
    result = None  # {columns, rows, truncated}

    if form.validate_on_submit():
        clean, error = sql_console.validate_select(form.sql.data)
        if error:
            flash(error, "danger")
        else:
            export = request.form.get("action") == "export"
            try:
                cols, rows, truncated = sql_console.run_query(
                    get_engine(), clean, limit=(sql_console.EXPORT_CAP if export else 500))
            except Exception as exc:  # noqa: BLE001 - surface the DB error
                flash(f"Query error: {exc}", "danger")
            else:
                if export:
                    return Response(
                        sql_console.to_csv(cols, rows), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=query.csv"})
                result = {"columns": cols, "rows": rows, "truncated": truncated}

    return render_template("designer/query.html", form=form, tables=tables, result=result)


# --------------------------------------------------------------------------- #
# Reports (group-by / aggregation) — designer sees all rows (no scoping)
# --------------------------------------------------------------------------- #
@bp.route("/report")
def report():
    session = _s()
    tables = _tables(session)
    if not tables:
        flash("Create a table first.", "info")
        return redirect(url_for("designer.dashboard"))
    table_id = request.args.get("table_id", type=int)
    table = session.get(MetaTable, table_id) if table_id else None
    if table is None:
        table = tables[0]
    ctx = reporting.build(session, engine_for_table(table), table, request.args, base_filters=None)
    if request.args.get("export") == "csv":
        return Response(
            reporting.to_csv(ctx["result"]), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={table.phys_name}_report.csv"})
    saved = session.scalars(select(ReportDef).where(
        ReportDef.user_id == current_user.id, ReportDef.table_id == table.id)
        .order_by(ReportDef.name)).all()
    current_query = urlencode([(k, v) for k, v in request.args.items(multi=True) if k != "export"])
    return render_template("designer/report.html",
                           action=url_for("designer.report"), tables=tables,
                           saved_reports=saved, current_query=current_query, **ctx)


# --------------------------------------------------------------------------- #
# Status workflows
# --------------------------------------------------------------------------- #
@bp.route("/workflows")
def workflows():
    session = _s()
    items, used = [], set()
    for w in session.scalars(select(Workflow).order_by(Workflow.id)):
        used.add(w.field_id)
        field = session.get(MetaField, w.field_id)
        items.append({"wf": w, "field": field, "table": session.get(MetaTable, w.table_id),
                      "states": json.loads(field.enum_options or "[]") if field else [],
                      "n_transitions": len(workflow.transitions(w))})
    candidates = []
    for t in _tables(session):
        ef = [f for f in t.fields if f.data_type == "enum" and f.id not in used]
        if ef:
            candidates.append({"table": t, "fields": ef})
    return render_template("designer/workflows.html", items=items, candidates=candidates)


@bp.route("/workflows", methods=["POST"])
def workflow_create():
    session = _s()
    field_id = request.form.get("field_id", type=int)
    field = session.get(MetaField, field_id) if field_id else None
    if not field or field.data_type != "enum":
        flash("Choose an enum field to build a workflow on.", "warning")
        return redirect(url_for("designer.workflows"))
    if session.scalar(select(Workflow).where(Workflow.field_id == field_id)):
        flash("That field already has a workflow.", "info")
        return redirect(url_for("designer.workflows"))
    states = json.loads(field.enum_options or "[]")
    wf = Workflow(table_id=field.table_id, field_id=field_id,
                  initial_state=(states[0] if states else None),
                  transitions="[]", layout="{}")
    session.add(wf)
    session.commit()
    return redirect(url_for("designer.workflow_edit", wf_id=wf.id))


@bp.route("/workflows/<int:wf_id>")
def workflow_edit(wf_id):
    session = _s()
    wf = session.get(Workflow, wf_id)
    if not wf:
        flash("Workflow not found.", "danger")
        return redirect(url_for("designer.workflows"))
    field = session.get(MetaField, wf.field_id)
    table = session.get(MetaTable, wf.table_id)
    helpers.ensure_roles(session)
    # all roles, incl. builtin designer/user — gating a transition to ["designer"]
    # legitimately restricts it to designers (unlike the per-form permissions page)
    roles = [r.name for r in session.scalars(select(Role).order_by(Role.name))]
    graph = {
        "states": json.loads(field.enum_options or "[]"),
        "layout": workflow.layout(wf),
        "transitions": workflow.transitions(wf),
        "initial": wf.initial_state,
        "roles": roles,
        "save_url": url_for("designer.workflow_save", wf_id=wf.id),
    }
    return render_template("designer/workflow_edit.html", wf=wf, field=field, table=table,
                           graph=graph)


@bp.route("/workflows/<int:wf_id>", methods=["POST"])
def workflow_save(wf_id):
    session = _s()
    wf = session.get(Workflow, wf_id)
    if not wf:
        return jsonify(ok=False, error="Not found."), 404
    field = session.get(MetaField, wf.field_id)
    states = set(json.loads(field.enum_options or "[]"))
    helpers.ensure_roles(session)
    valid_roles = {r.name for r in session.scalars(select(Role))}  # incl. custom roles
    data = request.get_json(silent=True) or {}
    trans = []
    for t in data.get("transitions", []):
        if isinstance(t, dict) and t.get("from") in states and t.get("to") in states:
            roles = [r for r in (t.get("roles") or []) if r in valid_roles]
            trans.append({"from": t["from"], "to": t["to"], "roles": roles})
    layout = {}
    for st, pos in (data.get("layout") or {}).items():
        if st in states and isinstance(pos, dict):
            try:
                layout[st] = {"x": float(pos.get("x", 0)), "y": float(pos.get("y", 0))}
            except (TypeError, ValueError):
                pass
    wf.transitions = json.dumps(trans)
    wf.layout = json.dumps(layout)
    wf.initial_state = data.get("initial") if data.get("initial") in states else None
    session.commit()
    return jsonify(ok=True)


@bp.route("/workflows/<int:wf_id>/delete", methods=["POST"])
def workflow_delete(wf_id):
    session = _s()
    wf = session.get(Workflow, wf_id)
    if wf:
        session.delete(wf)
        session.commit()
        flash("Workflow deleted.", "info")
    return redirect(url_for("designer.workflows"))


# --------------------------------------------------------------------------- #
# Data sources (other databases)
# --------------------------------------------------------------------------- #
@bp.route("/sources")
def sources():
    session = _s()
    items = session.scalars(select(DataSource).order_by(DataSource.name)).all()
    counts = {d.id: session.scalar(select(func.count()).select_from(MetaTable)
                                   .where(MetaTable.source_id == d.id)) for d in items}
    return render_template("designer/datasources.html", items=items, counts=counts,
                           form=DataSourceForm())


@bp.route("/sources", methods=["POST"])
def source_create():
    session = _s()
    form = DataSourceForm()
    if not form.validate_on_submit():
        flash("Invalid data source input.", "danger")
        return redirect(url_for("designer.sources"))
    session.add(DataSource(
        name=form.name.data, driver=form.driver.data or "mysql+pymysql",
        host=form.host.data or None, port=form.port.data, username=form.username.data or None,
        password=form.password.data or None, database=form.database.data or None,
        active=form.active.data))
    session.commit()
    flash("Data source added.", "success")
    return redirect(url_for("designer.sources"))


@bp.route("/sources/<int:source_id>", methods=["GET", "POST"])
def source_edit(source_id):
    session = _s()
    ds = session.get(DataSource, source_id)
    if not ds:
        flash("Data source not found.", "danger")
        return redirect(url_for("designer.sources"))
    form = DataSourceForm(obj=ds)
    if request.method == "GET":
        form.password.data = ""           # never echo the secret
    if form.validate_on_submit():
        ds.name = form.name.data
        ds.driver = form.driver.data or "mysql+pymysql"
        ds.host = form.host.data or None
        ds.port = form.port.data
        ds.username = form.username.data or None
        ds.database = form.database.data or None
        ds.active = form.active.data
        if form.password.data:            # blank keeps the existing password
            ds.password = form.password.data
        session.commit()
        flash("Data source saved.", "success")
        return redirect(url_for("designer.sources"))
    return render_template("designer/datasource_form.html", form=form, ds=ds)


@bp.route("/sources/<int:source_id>/test", methods=["POST"])
def source_test(source_id):
    session = _s()
    ds = session.get(DataSource, source_id)
    if ds:
        from datetime import datetime, timezone
        ok, detail = test_source(ds)
        ds.last_check_at = datetime.now(timezone.utc).replace(tzinfo=None)
        ds.last_status = "OK" if ok else f"Failed: {detail}"[:255]
        session.commit()
        flash(ds.last_status, "success" if ok else "danger")
    return redirect(url_for("designer.sources"))


@bp.route("/sources/<int:source_id>/delete", methods=["POST"])
def source_delete(source_id):
    session = _s()
    ds = session.get(DataSource, source_id)
    if ds:
        if session.scalar(select(MetaTable.id).where(MetaTable.source_id == source_id).limit(1)):
            flash("Some tables still use this data source — move or remove them first.", "warning")
            return redirect(url_for("designer.sources"))
        session.delete(ds)
        session.commit()
        flash("Data source deleted.", "info")
    return redirect(url_for("designer.sources"))


# --------------------------------------------------------------------------- #
# Integrations: connections (remote peers)
# --------------------------------------------------------------------------- #
@bp.route("/connections")
def connections():
    session = _s()
    items = session.scalars(select(Connection).order_by(Connection.name)).all()
    return render_template("designer/connections.html", items=items, form=ConnectionForm())


@bp.route("/connections", methods=["POST"])
def connection_create():
    session = _s()
    form = ConnectionForm()
    if not form.validate_on_submit():
        flash("Invalid connection input.", "danger")
        return redirect(url_for("designer.connections"))
    conn = Connection(name=form.name.data, base_url=form.base_url.data,
                      token=form.token.data or None, active=form.active.data)
    session.add(conn)
    session.commit()
    flash("Connection added.", "success")
    return redirect(url_for("designer.connections"))


@bp.route("/connections/<int:conn_id>", methods=["GET", "POST"])
def connection_edit(conn_id):
    session = _s()
    conn = session.get(Connection, conn_id)
    if not conn:
        flash("Connection not found.", "danger")
        return redirect(url_for("designer.connections"))
    form = ConnectionForm(obj=conn)
    if request.method == "GET":
        form.token.data = ""  # never echo the secret
    if form.validate_on_submit():
        conn.name = form.name.data
        conn.base_url = form.base_url.data
        conn.active = form.active.data
        if form.token.data:               # blank keeps the existing token
            conn.token = form.token.data
        session.commit()
        flash("Connection saved.", "success")
        return redirect(url_for("designer.connections"))
    return render_template("designer/connection_form.html", form=form, conn=conn)


@bp.route("/connections/<int:conn_id>/test", methods=["POST"])
def connection_test(conn_id):
    session = _s()
    conn = session.get(Connection, conn_id)
    if conn:
        from datetime import datetime, timezone
        ok, detail, tables = connectors.ping(conn)
        conn.last_check_at = datetime.now(timezone.utc).replace(tzinfo=None)
        conn.last_status = ("OK — " + ", ".join(tables[:20])) if ok else f"Failed: {detail}"
        session.commit()
        flash(conn.last_status, "success" if ok else "danger")
    return redirect(url_for("designer.connections"))


@bp.route("/connections/<int:conn_id>/delete", methods=["POST"])
def connection_delete(conn_id):
    session = _s()
    conn = session.get(Connection, conn_id)
    if conn:
        session.delete(conn)
        session.commit()
        flash("Connection deleted.", "info")
    return redirect(url_for("designer.connections"))


# --------------------------------------------------------------------------- #
# Integrations: feeds (chain a local table to a remote peer)
# --------------------------------------------------------------------------- #
@bp.route("/feeds")
def feeds_list():
    session = _s()
    items = [{"feed": f, "table": session.get(MetaTable, f.source_table_id),
              "conn": session.get(Connection, f.connection_id)}
             for f in session.scalars(select(Feed).order_by(Feed.source_table_id, Feed.id))]
    return render_template("designer/feeds.html", items=items, tables=_tables(session),
                           connections=session.scalars(
                               select(Connection).order_by(Connection.name)).all())


@bp.route("/feeds", methods=["POST"])
def feed_create():
    session = _s()
    table = session.get(MetaTable, request.form.get("table_id", type=int))
    conn = session.get(Connection, request.form.get("connection_id", type=int))
    if not table or not conn:
        flash("Pick a source table and a connection.", "warning")
        return redirect(url_for("designer.feeds_list"))
    feed = Feed(name=f"{table.label} → {conn.name}", source_table_id=table.id,
                connection_id=conn.id, target_table=table.phys_name, mode="create",
                event="create", active=True)
    session.add(feed)
    session.commit()
    return redirect(url_for("designer.feed_edit", feed_id=feed.id))


def _feed_choices(session, form, table):
    fields = list(table.fields)
    form.connection_id.choices = [(c.id, c.name) for c in session.scalars(
        select(Connection).order_by(Connection.name))]
    form.field_id.choices = [(0, "— none —")] + [
        (f.id, f.label) for f in fields if f.data_type == "enum"]
    form.cond_field_id.choices = [(0, "— none —")] + [(f.id, f.label) for f in fields]


def _source_columns(table):
    return ["id"] + [f.phys_name for f in table.fields
                     if f.data_type not in (RELATION_TYPE, "file", "image")]


@bp.route("/feeds/<int:feed_id>", methods=["GET", "POST"])
def feed_edit(feed_id):
    session = _s()
    feed = session.get(Feed, feed_id)
    if not feed:
        flash("Feed not found.", "danger")
        return redirect(url_for("designer.feeds_list"))
    table = session.get(MetaTable, feed.source_table_id)
    form = FeedForm(obj=feed)
    _feed_choices(session, form, table)

    if request.method == "GET":
        form.event.data = feed.event or ""
        form.mode.data = feed.mode or "create"
        form.cond_op.data = feed.cond_op or ""
        form.field_id.data = feed.field_id or 0
        form.cond_field_id.data = feed.cond_field_id or 0
        form.connection_id.data = feed.connection_id

    if form.validate_on_submit():
        feed.name = form.name.data
        feed.active = form.active.data
        feed.connection_id = form.connection_id.data
        feed.target_table = form.target_table.data
        feed.mode = form.mode.data
        feed.match_target_field = form.match_target_field.data or None
        feed.event = form.event.data or None
        feed.field_id = form.field_id.data or None
        feed.from_state = form.from_state.data or None
        feed.to_state = form.to_state.data or None
        feed.cond_field_id = form.cond_field_id.data or None
        feed.cond_op = form.cond_op.data or None
        feed.cond_value = form.cond_value.data or None
        feed.schedule_minutes = form.schedule_minutes.data or None
        feed.allow_manual = form.allow_manual.data
        feed.skip_api_writes = form.skip_api_writes.data
        targets = request.form.getlist("map_target")
        sources = request.form.getlist("map_source")
        feed.field_map = json.dumps([{"target": t.strip(), "source": s.strip()}
                                     for t, s in zip(targets, sources)
                                     if t.strip() and s.strip()])
        session.commit()
        flash("Feed saved.", "success")
        return redirect(url_for("designer.feeds_list"))

    conn = session.get(Connection, feed.connection_id)
    remote = [f["name"] for f in connectors.remote_fields(conn, feed.target_table)] if conn else []
    return render_template("designer/feed_form.html", form=form, feed=feed, table=table,
                           field_map=json.loads(feed.field_map or "[]"),
                           source_columns=_source_columns(table), remote_fields=remote)


@bp.route("/feeds/<int:feed_id>/run", methods=["POST"])
def feed_run(feed_id):
    session = _s()
    feed = session.get(Feed, feed_id)
    if feed:
        pushed = feeds.run_scheduled(session, get_engine(), only_feed_id=feed.id)
        flash(f"Pushed {pushed} row(s).", "success")
    return redirect(url_for("designer.feeds_list"))


@bp.route("/feeds/<int:feed_id>/delete", methods=["POST"])
def feed_delete(feed_id):
    session = _s()
    feed = session.get(Feed, feed_id)
    if feed:
        session.delete(feed)
        session.commit()
        flash("Feed deleted.", "info")
    return redirect(url_for("designer.feeds_list"))


# --------------------------------------------------------------------------- #
# Integrations: inbound webhooks (receive events from external systems)
# --------------------------------------------------------------------------- #
def _mint_webhook_token():
    """Return ``(raw, token_hash, prefix)`` for a new webhook secret."""
    import secrets

    from ..api.tokens import hash_token
    raw = "whk_" + secrets.token_urlsafe(32)
    return raw, hash_token(raw), raw[:12]


def _webhook_url(raw):
    return url_for("hooks.receive", token=raw, _external=True)


@bp.route("/webhooks")
def webhooks():
    session = _s()
    items = [{"wh": w, "table": session.get(MetaTable, w.target_table_id)}
             for w in session.scalars(
                 select(Webhook).order_by(Webhook.target_table_id, Webhook.id))]
    return render_template("designer/webhooks.html", items=items, tables=_tables(session))


@bp.route("/webhooks", methods=["POST"])
def webhook_create():
    session = _s()
    table = session.get(MetaTable, request.form.get("table_id", type=int))
    if not table:
        flash("Pick a target table.", "warning")
        return redirect(url_for("designer.webhooks"))
    raw, token_hash, prefix = _mint_webhook_token()
    wh = Webhook(name=f"Inbound → {table.label}", target_table_id=table.id,
                 token_hash=token_hash, prefix=prefix, mode="create", active=True)
    session.add(wh)
    session.commit()
    web_session["wh_new_url"] = _webhook_url(raw)   # shown once on the edit page
    return redirect(url_for("designer.webhook_edit", webhook_id=wh.id))


def _webhook_target_columns(table):
    return [f.phys_name for f in table.fields
            if f.data_type not in (RELATION_TYPE, "file", "image")]


@bp.route("/webhooks/<int:webhook_id>", methods=["GET", "POST"])
def webhook_edit(webhook_id):
    session = _s()
    wh = session.get(Webhook, webhook_id)
    if not wh:
        flash("Webhook not found.", "danger")
        return redirect(url_for("designer.webhooks"))
    table = session.get(MetaTable, wh.target_table_id)
    form = WebhookForm(obj=wh)
    form.user_id.choices = [(0, "— system (no owner) —")] + [
        (u.id, u.username) for u in session.scalars(select(AppUser).order_by(AppUser.username))]

    if request.method == "GET":
        form.mode.data = wh.mode or "create"
        form.user_id.data = wh.user_id or 0
        form.secret.data = ""                       # never echo the secret

    if form.validate_on_submit():
        wh.name = form.name.data
        wh.active = form.active.data
        wh.mode = form.mode.data
        wh.match_field = form.match_field.data or None
        wh.user_id = form.user_id.data or None
        if form.secret.data:                        # blank keeps the existing secret
            wh.secret = form.secret.data
        wh.max_body_bytes = form.max_body_bytes.data if form.max_body_bytes.data is not None else None
        wh.rate_limit = form.rate_limit.data if form.rate_limit.data is not None else None
        wh.rate_window = form.rate_window.data if form.rate_window.data is not None else None
        targets = request.form.getlist("map_target")
        sources = request.form.getlist("map_source")
        wh.field_map = json.dumps([{"target": t.strip(), "source": s.strip()}
                                   for t, s in zip(targets, sources)
                                   if t.strip() and s.strip()])
        session.commit()
        flash("Webhook saved.", "success")
        return redirect(url_for("designer.webhooks"))

    cfg = current_app.config
    return render_template("designer/webhook_form.html", form=form, wh=wh, table=table,
                           field_map=json.loads(wh.field_map or "[]"),
                           target_columns=_webhook_target_columns(table),
                           new_url=web_session.pop("wh_new_url", None),
                           has_secret=bool(wh.secret),
                           defaults={"body": cfg.get("WEBHOOK_MAX_BODY_BYTES"),
                                     "rate": cfg.get("WEBHOOK_RATE_LIMIT"),
                                     "window": cfg.get("WEBHOOK_RATE_WINDOW")})


@bp.route("/webhooks/<int:webhook_id>/rotate", methods=["POST"])
def webhook_rotate(webhook_id):
    session = _s()
    wh = session.get(Webhook, webhook_id)
    if wh:
        raw, token_hash, prefix = _mint_webhook_token()
        wh.token_hash, wh.prefix = token_hash, prefix
        session.commit()
        web_session["wh_new_url"] = _webhook_url(raw)
        flash("Token rotated — the old URL no longer works.", "success")
    return redirect(url_for("designer.webhook_edit", webhook_id=webhook_id))


@bp.route("/webhooks/<int:webhook_id>/delete", methods=["POST"])
def webhook_delete(webhook_id):
    session = _s()
    wh = session.get(Webhook, webhook_id)
    if wh:
        session.delete(wh)
        session.commit()
        flash("Webhook deleted.", "info")
    return redirect(url_for("designer.webhooks"))


# --------------------------------------------------------------------------- #
# Integrations: pull sources (poll a remote source → upsert into a local table)
# --------------------------------------------------------------------------- #
@bp.route("/pulls")
def pulls():
    session = _s()
    items = [{"src": p, "table": session.get(MetaTable, p.target_table_id),
              "conn": session.get(Connection, p.connection_id) if p.connection_id else None}
             for p in session.scalars(select(PullSource).order_by(PullSource.id))]
    return render_template("designer/pull_sources.html", items=items, tables=_tables(session))


@bp.route("/pulls", methods=["POST"])
def pull_create():
    session = _s()
    table = session.get(MetaTable, request.form.get("table_id", type=int))
    if not table:
        flash("Pick a target table.", "warning")
        return redirect(url_for("designer.pulls"))
    src = PullSource(name=f"Pull → {table.label}", target_table_id=table.id,
                     kind="peer", mode="upsert", active=True)
    session.add(src)
    session.commit()
    return redirect(url_for("designer.pull_edit", source_id=src.id))


def _pull_choices(session, form):
    form.connection_id.choices = [(0, "— none —")] + [(c.id, c.name) for c in session.scalars(
        select(Connection).order_by(Connection.name))]
    form.user_id.choices = [(0, "— system (no owner) —")] + [
        (u.id, u.username) for u in session.scalars(select(AppUser).order_by(AppUser.username))]


def _pull_config_load(form, cfg):
    """Prefill the advanced form fields + the raw-JSON escape hatch from ``config``."""
    a = cfg.get("auth") or {}
    form.auth_type.data = a.get("type") or "none"
    form.auth_header.data, form.auth_username.data = a.get("header") or "", a.get("username") or ""
    form.auth_param.data = a.get("param") or ""
    req = cfg.get("request") or {}
    form.http_method.data = req.get("method") or "GET"
    form.request_body.data = req.get("body") or ""
    pg = cfg.get("pagination") or {}
    form.pagination_style.data = pg.get("style") or "none"
    form.page_param.data, form.size_param.data = pg.get("param") or "", pg.get("size_param") or ""
    form.page_start.data, form.next_path.data = pg.get("start"), pg.get("next_path") or ""
    form.max_pages.data = pg.get("max_pages")
    form.cursor_type.data = (cfg.get("cursor") or {}).get("type") or ""
    f = cfg.get("filter") or {}
    form.filter_field.data, form.filter_op.data = f.get("field") or "", f.get("op") or ""
    form.filter_value.data = f.get("value") or ""
    # raw = everything the structured fields don't manage (request.params/headers, transforms, …)
    leftover = {k: v for k, v in cfg.items()
                if k not in ("auth", "pagination", "cursor", "filter")}
    r = {k: v for k, v in req.items() if k not in ("method", "body")}
    leftover["request"] = r if r else leftover.pop("request", None)
    leftover = {k: v for k, v in leftover.items() if v}
    form.config_raw.data = json.dumps(leftover, indent=2) if leftover else ""


def _pull_config_save(form):
    """Assemble the config JSON: parse the raw escape hatch, then overlay structured fields."""
    try:
        cfg = json.loads(form.config_raw.data) if (form.config_raw.data or "").strip() else {}
        if not isinstance(cfg, dict):
            cfg = {}
    except ValueError:
        cfg = {}
    if form.auth_type.data and form.auth_type.data != "none":
        auth = {"type": form.auth_type.data}
        for key, fld in (("header", form.auth_header), ("username", form.auth_username),
                         ("param", form.auth_param)):
            if fld.data:
                auth[key] = fld.data
        cfg["auth"] = auth
    else:
        cfg.pop("auth", None)
    req = cfg.get("request") or {}
    req["method"] = form.http_method.data or "GET"
    if form.request_body.data:
        req["body"] = form.request_body.data
    else:
        req.pop("body", None)
    cfg["request"] = req
    if form.pagination_style.data and form.pagination_style.data != "none":
        pg = {"style": form.pagination_style.data}
        for key, fld in (("param", form.page_param), ("size_param", form.size_param),
                         ("start", form.page_start), ("next_path", form.next_path),
                         ("max_pages", form.max_pages)):
            if fld.data not in (None, ""):
                pg[key] = fld.data
        cfg["pagination"] = pg
    else:
        cfg.pop("pagination", None)
    if form.cursor_type.data:
        cfg["cursor"] = {"type": form.cursor_type.data}
    else:
        cfg.pop("cursor", None)
    if form.filter_field.data and form.filter_op.data:
        cfg["filter"] = {"field": form.filter_field.data, "op": form.filter_op.data,
                         "value": form.filter_value.data or ""}
    else:
        cfg.pop("filter", None)
    cfg = {k: v for k, v in cfg.items() if v not in (None, {}, "")}
    return json.dumps(cfg) if cfg else None


@bp.route("/pulls/<int:source_id>", methods=["GET", "POST"])
def pull_edit(source_id):
    session = _s()
    src = session.get(PullSource, source_id)
    if not src:
        flash("Pull source not found.", "danger")
        return redirect(url_for("designer.pulls"))
    table = session.get(MetaTable, src.target_table_id)
    form = PullSourceForm(obj=src)
    _pull_choices(session, form)

    if request.method == "GET":
        form.kind.data = src.kind or "peer"
        form.mode.data = src.mode or "upsert"
        form.connection_id.data = src.connection_id or 0
        form.user_id.data = src.user_id or 0
        form.headers.data = ""                       # never echo the secret headers
        form.auth_secret.data = ""                   # never echo the secret
        try:
            _pull_config_load(form, json.loads(src.config) if src.config else {})
        except ValueError:
            _pull_config_load(form, {})

    if form.validate_on_submit():
        src.name = form.name.data
        src.active = form.active.data
        src.kind = form.kind.data
        src.connection_id = form.connection_id.data or None
        src.remote_table = form.remote_table.data or None
        src.url = form.url.data or None
        if form.headers.data:                        # blank keeps existing headers
            src.headers = form.headers.data
        if form.auth_secret.data:                    # blank keeps existing secret
            src.auth_secret = form.auth_secret.data
        src.records_path = form.records_path.data or None
        src.mode = form.mode.data
        src.match_field = form.match_field.data or None
        src.cursor_field = form.cursor_field.data or None
        src.page_size = form.page_size.data or None
        src.schedule_minutes = form.schedule_minutes.data or None
        src.user_id = form.user_id.data or None
        src.config = _pull_config_save(form)
        targets = request.form.getlist("map_target")
        sources = request.form.getlist("map_source")
        src.field_map = json.dumps([{"target": t.strip(), "source": s.strip()}
                                    for t, s in zip(targets, sources)
                                    if t.strip() and s.strip()])
        session.commit()
        flash("Pull source saved.", "success")
        return redirect(url_for("designer.pulls"))

    conn = session.get(Connection, src.connection_id) if src.connection_id else None
    remote = [f["name"] for f in connectors.remote_fields(conn, src.remote_table)] \
        if conn and src.remote_table else []
    local_cols = [f.phys_name for f in table.fields
                  if f.data_type not in (RELATION_TYPE, "file", "image")]
    return render_template("designer/pull_source_form.html", form=form, src=src, table=table,
                           field_map=json.loads(src.field_map or "[]"),
                           local_columns=local_cols, remote_fields=remote,
                           has_headers=bool(src.headers))


@bp.route("/pulls/<int:source_id>/run", methods=["POST"])
def pull_run(source_id):
    session = _s()
    src = session.get(PullSource, source_id)
    if src:
        n = pull.run_one(session, get_engine(), src)
        flash(f"Pulled {n} record(s).", "success")
    return redirect(url_for("designer.pulls"))


@bp.route("/pulls/<int:source_id>/delete", methods=["POST"])
def pull_delete(source_id):
    session = _s()
    src = session.get(PullSource, source_id)
    if src:
        session.delete(src)
        session.commit()
        flash("Pull source deleted.", "info")
    return redirect(url_for("designer.pulls"))


# --------------------------------------------------------------------------- #
# Shared dashboards (designer-built; owner_user_id NULL)
# --------------------------------------------------------------------------- #
@bp.route("/dashboards")
def dashboards_list():
    session = _s()
    items = session.scalars(select(Dashboard).where(Dashboard.owner_user_id.is_(None))
                            .order_by(Dashboard.position, Dashboard.id)).all()
    return render_template("designer/dashboards.html", items=items, form=DashboardForm())


@bp.route("/dashboards", methods=["POST"])
def dashboard_create():
    session = _s()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name a dashboard.", "warning")
        return redirect(url_for("designer.dashboards_list"))
    dash = Dashboard(name=name, owner_user_id=None)
    session.add(dash)
    session.commit()
    return redirect(url_for("designer.dashboard_edit", dash_id=dash.id))


def _widget_choices(session, form):
    form.table_id.choices = [(0, "— none (text tile) —")] + [
        (t.id, t.label) for t in _tables(session)]


@bp.route("/dashboards/<int:dash_id>", methods=["GET", "POST"])
def dashboard_edit(dash_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    if not dash or dash.owner_user_id is not None:
        flash("Dashboard not found.", "danger")
        return redirect(url_for("designer.dashboards_list"))
    form = DashboardForm(obj=dash)
    if request.method == "GET":
        form.columns.data = str(dash.columns or 2)
    if form.validate_on_submit():
        dash.name = form.name.data
        dash.description = form.description.data or None
        dash.columns = int(form.columns.data or 2)
        session.commit()
        flash("Dashboard saved.", "success")
        return redirect(url_for("designer.dashboard_edit", dash_id=dash.id))
    wform = DashboardWidgetForm()
    _widget_choices(session, wform)
    return render_template("designer/dashboard_form.html", dash=dash, form=form, wform=wform,
                           widgets=dash.widgets, view_url=url_for("user.dashboard_view", dash_id=dash.id))


@bp.route("/dashboards/<int:dash_id>/delete", methods=["POST"])
def dashboard_delete(dash_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    if dash and dash.owner_user_id is None:
        session.delete(dash)
        session.commit()
        flash("Dashboard deleted.", "info")
    return redirect(url_for("designer.dashboards_list"))


def _save_widget(session, w, form):
    w.title = form.title.data or None
    w.kind = form.kind.data
    w.table_id = form.table_id.data or None
    w.query = form.query.data or None
    w.chart_type = form.chart_type.data or "bar"
    w.content = form.content.data or None
    w.width = int(form.width.data or 1)
    w.limit = form.limit.data or 5


@bp.route("/dashboards/<int:dash_id>/widgets", methods=["POST"])
def widget_add(dash_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    if not dash:
        return redirect(url_for("designer.dashboards_list"))
    form = DashboardWidgetForm()
    _widget_choices(session, form)
    if form.validate_on_submit():
        pos = (max([x.position for x in dash.widgets], default=-1)) + 1
        w = DashboardWidget(dashboard_id=dash.id, position=pos)
        _save_widget(session, w, form)
        session.add(w)
        session.commit()
        flash("Widget added.", "success")
    return redirect(url_for("designer.dashboard_edit", dash_id=dash_id))


@bp.route("/dashboards/widgets/<int:widget_id>", methods=["GET", "POST"])
def widget_edit(widget_id):
    session = _s()
    w = session.get(DashboardWidget, widget_id)
    if not w:
        flash("Widget not found.", "danger")
        return redirect(url_for("designer.dashboards_list"))
    form = DashboardWidgetForm(obj=w)
    _widget_choices(session, form)
    if request.method == "GET":
        form.table_id.data = w.table_id or 0
        form.width.data = str(w.width or 1)
    if form.validate_on_submit():
        _save_widget(session, w, form)
        session.commit()
        flash("Widget saved.", "success")
        return redirect(url_for("designer.dashboard_edit", dash_id=w.dashboard_id))
    return render_template("designer/widget_form.html", form=form, w=w)


@bp.route("/dashboards/widgets/<int:widget_id>/delete", methods=["POST"])
def widget_delete(widget_id):
    session = _s()
    w = session.get(DashboardWidget, widget_id)
    dash_id = w.dashboard_id if w else None
    if w:
        session.delete(w)
        session.commit()
        flash("Widget removed.", "info")
    return redirect(url_for("designer.dashboard_edit", dash_id=dash_id) if dash_id
                    else url_for("designer.dashboards_list"))


@bp.route("/dashboards/widgets/<int:widget_id>/move", methods=["POST"])
def widget_move(widget_id):
    session = _s()
    w = session.get(DashboardWidget, widget_id)
    if w:
        sibs = sorted(session.get(Dashboard, w.dashboard_id).widgets, key=lambda x: x.position)
        i = sibs.index(w)
        j = i - 1 if request.form.get("dir") == "up" else i + 1
        if 0 <= j < len(sibs):
            sibs[i].position, sibs[j].position = sibs[j].position, sibs[i].position
            session.commit()
    return redirect(url_for("designer.dashboard_edit", dash_id=w.dashboard_id) if w
                    else url_for("designer.dashboards_list"))


# --------------------------------------------------------------------------- #
# Triggers & notifications
# --------------------------------------------------------------------------- #
@bp.route("/triggers")
def triggers():
    session = _s()
    items = [{"rule": r, "table": session.get(MetaTable, r.table_id)}
             for r in session.scalars(select(TriggerRule).order_by(TriggerRule.table_id,
                                                                   TriggerRule.id))]
    return render_template("designer/triggers.html", items=items, tables=_tables(session))


@bp.route("/triggers", methods=["POST"])
def trigger_create():
    session = _s()
    table = session.get(MetaTable, request.form.get("table_id", type=int))
    if not table:
        flash("Choose a table.", "warning")
        return redirect(url_for("designer.triggers"))
    tr = TriggerRule(table_id=table.id, name="New rule", event="update", active=True,
                     notify_target="actor")
    session.add(tr)
    session.commit()
    return redirect(url_for("designer.trigger_edit", rule_id=tr.id))


def _trigger_choices(session, form, table):
    fields = list(table.fields)
    form.field_id.choices = [(0, "— none —")] + [
        (f.id, f.label) for f in fields if f.data_type == "enum"]
    form.cond_field_id.choices = [(0, "— none —")] + [(f.id, f.label) for f in fields]
    form.set_field_id.choices = [(0, "— none —")] + [
        (f.id, f.label) for f in fields if f.data_type not in (RELATION_TYPE, "file", "image")]
    form.notify_user_id.choices = [(0, "— none —")] + [
        (u.id, u.username) for u in session.scalars(select(AppUser).order_by(AppUser.username))]
    form.create_table_id.choices = [(0, "— none —")] + [
        (t.id, t.label) for t in _tables(session)]


@bp.route("/triggers/<int:rule_id>", methods=["GET", "POST"])
def trigger_edit(rule_id):
    session = _s()
    tr = session.get(TriggerRule, rule_id)
    if not tr:
        flash("Trigger not found.", "danger")
        return redirect(url_for("designer.triggers"))
    table = session.get(MetaTable, tr.table_id)
    form = TriggerRuleForm(obj=tr)
    _trigger_choices(session, form, table)

    if request.method == "GET":
        form.event.data = tr.event
        form.notify_target.data = tr.notify_target or "actor"
        form.cond_op.data = tr.cond_op or ""
        form.field_id.data = tr.field_id or 0
        form.cond_field_id.data = tr.cond_field_id or 0
        form.set_field_id.data = tr.set_field_id or 0
        form.notify_user_id.data = tr.notify_user_id or 0
        form.create_table_id.data = tr.create_table_id or 0
        form.webhook_format.data = tr.webhook_format or "json"

    if form.validate_on_submit():
        tr.name = form.name.data
        tr.active = form.active.data
        tr.event = form.event.data
        tr.field_id = form.field_id.data or None
        tr.from_state = form.from_state.data or None
        tr.to_state = form.to_state.data or None
        tr.cond_field_id = form.cond_field_id.data or None
        tr.cond_op = form.cond_op.data or None
        tr.cond_value = form.cond_value.data or None
        tr.in_app = form.in_app.data
        tr.notify_target = form.notify_target.data or None
        tr.notify_user_id = form.notify_user_id.data or None
        tr.message = form.message.data or None
        tr.email_to = form.email_to.data or None
        tr.email_subject = form.email_subject.data or None
        tr.email_body = form.email_body.data or None
        tr.webhook_url = form.webhook_url.data or None
        tr.set_field_id = form.set_field_id.data or None
        tr.set_value = form.set_value.data or None
        tr.create_table_id = form.create_table_id.data or None
        tr.create_field_map = form.create_field_map.data or None
        tr.webhook_format = form.webhook_format.data or None
        tr.schedule_minutes = form.schedule_minutes.data or None
        session.commit()
        flash("Trigger saved.", "success")
        return redirect(url_for("designer.triggers"))
    return render_template("designer/trigger_form.html", form=form, rule=tr, table=table)


@bp.route("/triggers/<int:rule_id>/delete", methods=["POST"])
def trigger_delete(rule_id):
    session = _s()
    tr = session.get(TriggerRule, rule_id)
    if tr:
        session.delete(tr)
        session.commit()
        flash("Trigger deleted.", "info")
    return redirect(url_for("designer.triggers"))


# --------------------------------------------------------------------------- #
# SLA policies
# --------------------------------------------------------------------------- #
@bp.route("/sla-policies")
def sla_policies():
    session = _s()
    items = [{"policy": p, "table": session.get(MetaTable, p.table_id)}
             for p in session.scalars(select(SlaPolicy).order_by(SlaPolicy.table_id,
                                                                 SlaPolicy.id))]
    return render_template("designer/sla_policies.html", items=items, tables=_tables(session))


@bp.route("/sla-policies", methods=["POST"])
def sla_policy_create():
    session = _s()
    table = session.get(MetaTable, request.form.get("table_id", type=int))
    if not table:
        flash("Choose a table.", "warning")
        return redirect(url_for("designer.sla_policies"))
    policy = SlaPolicy(table_id=table.id, name="New SLA", target_minutes=60, active=True,
                       start_on_create=True)
    session.add(policy)
    session.commit()
    return redirect(url_for("designer.sla_policy_edit", policy_id=policy.id))


def _sla_choices(session, form, table):
    fields = list(table.fields)
    enums = [(f.id, f.label) for f in fields if f.data_type == "enum"]
    dates = [(f.id, f.label) for f in fields if f.data_type in ("datetime", "date")]
    settable = [(f.id, f.label) for f in fields
                if f.data_type not in (RELATION_TYPE, "file", "image")]
    form.status_field_id.choices = [(0, "— none —")] + enums
    form.state_field_id.choices = [(0, "— none —")] + settable
    form.due_field_id.choices = [(0, "— none —")] + dates
    form.cond_field_id.choices = [(0, "— none —")] + [(f.id, f.label) for f in fields]
    form.breach_set_field_id.choices = [(0, "— none —")] + settable
    form.breach_notify_user_id.choices = [(0, "— none —")] + [
        (u.id, u.username) for u in session.scalars(select(AppUser).order_by(AppUser.username))]


@bp.route("/sla-policies/<int:policy_id>", methods=["GET", "POST"])
def sla_policy_edit(policy_id):
    session = _s()
    policy = session.get(SlaPolicy, policy_id)
    if not policy:
        flash("SLA policy not found.", "danger")
        return redirect(url_for("designer.sla_policies"))
    table = session.get(MetaTable, policy.table_id)
    form = SlaPolicyForm(obj=policy)
    _sla_choices(session, form, table)

    if request.method == "GET":
        form.cond_op.data = policy.cond_op or ""
        form.breach_notify_target.data = policy.breach_notify_target or ""
        for fld, val in [("status_field_id", policy.status_field_id),
                         ("state_field_id", policy.state_field_id),
                         ("due_field_id", policy.due_field_id),
                         ("cond_field_id", policy.cond_field_id),
                         ("breach_set_field_id", policy.breach_set_field_id),
                         ("breach_notify_user_id", policy.breach_notify_user_id)]:
            getattr(form, fld).data = val or 0

    if form.validate_on_submit():
        policy.name = form.name.data
        policy.active = form.active.data
        policy.target_minutes = form.target_minutes.data
        policy.warn_minutes = form.warn_minutes.data
        policy.status_field_id = form.status_field_id.data or None
        policy.start_on_create = form.start_on_create.data
        policy.start_states = form.start_states.data or None
        policy.pause_states = form.pause_states.data or None
        policy.stop_states = form.stop_states.data or None
        policy.cond_field_id = form.cond_field_id.data or None
        policy.cond_op = form.cond_op.data or None
        policy.cond_value = form.cond_value.data or None
        policy.state_field_id = form.state_field_id.data or None
        policy.due_field_id = form.due_field_id.data or None
        policy.breach_in_app = form.breach_in_app.data
        policy.breach_notify_target = form.breach_notify_target.data or None
        policy.breach_notify_user_id = form.breach_notify_user_id.data or None
        policy.breach_message = form.breach_message.data or None
        policy.breach_email_to = form.breach_email_to.data or None
        policy.breach_email_subject = form.breach_email_subject.data or None
        policy.breach_email_body = form.breach_email_body.data or None
        policy.breach_set_field_id = form.breach_set_field_id.data or None
        policy.breach_set_value = form.breach_set_value.data or None
        policy.escalations = form.escalations.data or None
        session.commit()
        flash("SLA policy saved.", "success")
        return redirect(url_for("designer.sla_policies"))
    return render_template("designer/sla_form.html", form=form, policy=policy, table=table)


@bp.route("/sla-policies/<int:policy_id>/delete", methods=["POST"])
def sla_policy_delete(policy_id):
    session = _s()
    policy = session.get(SlaPolicy, policy_id)
    if policy:
        session.delete(policy)
        session.commit()
        flash("SLA policy deleted.", "info")
    return redirect(url_for("designer.sla_policies"))


# --------------------------------------------------------------------------- #
# Reconciliation: merge duplicate records
# --------------------------------------------------------------------------- #
@bp.route("/reconcile")
def reconcile_home():
    session = _s()
    table = session.get(MetaTable, request.args.get("table_id", type=int) or 0)
    survivor = (request.args.get("survivor") or "").strip()
    duplicate = (request.args.get("duplicate") or "").strip()
    prev = None
    if table and survivor and duplicate:
        prev = reconcile.preview(session, engine_for_table(table), table, survivor, duplicate)
        if prev is None:
            flash("Both records must exist in the chosen table.", "warning")
    return render_template("designer/reconcile.html", tables=_tables(session), table=table,
                           survivor=survivor, duplicate=duplicate, preview=prev)


@bp.route("/reconcile", methods=["POST"])
def reconcile_merge():
    session = _s()
    table = session.get(MetaTable, request.form.get("table_id", type=int) or 0)
    survivor = (request.form.get("survivor") or "").strip()
    duplicate = (request.form.get("duplicate") or "").strip()
    if not table or not survivor or not duplicate:
        flash("Pick a table and both record ids.", "warning")
        return redirect(url_for("designer.reconcile_home"))
    try:
        summary = reconcile.merge(session, engine_for_table(table), table, survivor,
                                  duplicate, current_user.id)
        flash(f"Merged #{duplicate} into #{survivor}: {summary['repointed']} reference(s) "
              f"repointed, {summary['moved_links']} link(s) moved, "
              f"{summary['filled']} field(s) filled.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("designer.reconcile_home", table_id=table.id))


# --------------------------------------------------------------------------- #
# Approval steps (attached to workflow transitions)
# --------------------------------------------------------------------------- #
@bp.route("/approvals")
def approvals():
    session = _s()
    items = []
    for w in session.scalars(select(Workflow).order_by(Workflow.id)):
        n_steps = session.scalar(select(func.count()).select_from(ApprovalStep)
                                 .where(ApprovalStep.workflow_id == w.id)) or 0
        items.append({"wf": w, "field": session.get(MetaField, w.field_id),
                      "table": session.get(MetaTable, w.table_id),
                      "n_transitions": len(workflow.transitions(w)), "n_steps": n_steps})
    return render_template("designer/approvals.html", items=items)


@bp.route("/approvals/<int:workflow_id>")
def approval_steps(workflow_id):
    session = _s()
    wf = session.get(Workflow, workflow_id)
    if not wf:
        flash("Workflow not found.", "danger")
        return redirect(url_for("designer.approvals"))
    steps = session.scalars(select(ApprovalStep).where(ApprovalStep.workflow_id == wf.id)
                            .order_by(ApprovalStep.position, ApprovalStep.id)).all()
    by_trans = {}
    for s in steps:
        by_trans.setdefault((s.from_state, s.to_state), []).append(s)
    return render_template("designer/approval_steps.html", wf=wf,
                           table=session.get(MetaTable, wf.table_id),
                           field=session.get(MetaField, wf.field_id),
                           transitions=workflow.transitions(wf), by_trans=by_trans,
                           roles=[r.name for r in session.scalars(select(Role).order_by(Role.name))],
                           users=session.scalars(select(AppUser).order_by(AppUser.username)).all())


@bp.route("/approvals/<int:workflow_id>/steps", methods=["POST"])
def approval_step_add(workflow_id):
    session = _s()
    wf = session.get(Workflow, workflow_id)
    if not wf:
        abort(404)
    frm, to = request.form.get("from_state"), request.form.get("to_state")
    if not frm or not to:
        flash("Pick a transition.", "warning")
        return redirect(url_for("designer.approval_steps", workflow_id=workflow_id))
    session.add(ApprovalStep(
        workflow_id=wf.id, from_state=frm, to_state=to,
        position=request.form.get("position", type=int) or 1,
        name=request.form.get("name") or None,
        approver_role=request.form.get("approver_role") or None,
        approver_user_id=request.form.get("approver_user_id", type=int) or None))
    session.commit()
    flash("Approval step added.", "success")
    return redirect(url_for("designer.approval_steps", workflow_id=workflow_id))


@bp.route("/approval-steps/<int:step_id>/delete", methods=["POST"])
def approval_step_delete(step_id):
    session = _s()
    step = session.get(ApprovalStep, step_id)
    wfid = step.workflow_id if step else None
    if step:
        session.delete(step)
        session.commit()
        flash("Approval step deleted.", "info")
    return redirect(url_for("designer.approval_steps", workflow_id=wfid) if wfid
                    else url_for("designer.approvals"))


# --------------------------------------------------------------------------- #
# Scheduled jobs — one view over every time-driven trigger, feed and report
# --------------------------------------------------------------------------- #
@bp.route("/scheduled")
def scheduled_jobs():
    session = _s()
    jobs = []
    for r in session.scalars(select(TriggerRule).where(
            TriggerRule.event == "scheduled", TriggerRule.schedule_minutes.is_not(None))):
        t = session.get(MetaTable, r.table_id)
        jobs.append({"kind": "Trigger", "name": r.name, "where": t.label if t else "?",
                     "every": r.schedule_minutes, "active": r.active, "last": r.last_run_at,
                     "run_url": url_for("designer.scheduled_run_trigger", rule_id=r.id),
                     "edit_url": url_for("designer.trigger_edit", rule_id=r.id)})
    for f in session.scalars(select(Feed).where(Feed.schedule_minutes.is_not(None))):
        t = session.get(MetaTable, f.source_table_id)
        jobs.append({"kind": "Feed", "name": f.name, "where": t.label if t else "?",
                     "every": f.schedule_minutes, "active": f.active, "last": f.last_run_at,
                     "run_url": url_for("designer.feed_run", feed_id=f.id),
                     "edit_url": url_for("designer.feed_edit", feed_id=f.id)})
    for p in session.scalars(select(PullSource).where(PullSource.schedule_minutes.is_not(None))):
        t = session.get(MetaTable, p.target_table_id)
        jobs.append({"kind": "Pull", "name": p.name, "where": t.label if t else "?",
                     "every": p.schedule_minutes, "active": p.active, "last": p.last_run_at,
                     "run_url": url_for("designer.pull_run", source_id=p.id),
                     "edit_url": url_for("designer.pull_edit", source_id=p.id)})
    for rp in session.scalars(select(ReportDef).where(ReportDef.schedule_minutes.is_not(None))):
        t = session.get(MetaTable, rp.table_id)
        jobs.append({"kind": "Report", "name": rp.name, "where": t.label if t else "?",
                     "every": rp.schedule_minutes, "active": True, "last": rp.last_run_at,
                     "run_url": url_for("designer.scheduled_run_report", report_id=rp.id),
                     "edit_url": url_for("user.report", table_id=rp.table_id)})
    return render_template("designer/scheduled_jobs.html", jobs=jobs)


@bp.route("/scheduled/run-all", methods=["POST"])
def scheduled_run_all():
    s = scheduler.run_due(_s(), get_engine())
    flash("Ran due jobs — triggers: {triggers}, feeds: {feeds}, pulls: {pulls}, "
          "reports: {reports}.".format(**s), "success")
    return redirect(url_for("designer.scheduled_jobs"))


@bp.route("/scheduled/trigger/<int:rule_id>/run", methods=["POST"])
def scheduled_run_trigger(rule_id):
    n = scheduler.run_one_trigger(_s(), rule_id)
    flash(f"Ran scheduled trigger over {n} row(s).", "success")
    return redirect(url_for("designer.scheduled_jobs"))


@bp.route("/scheduled/report/<int:report_id>/run", methods=["POST"])
def scheduled_run_report(report_id):
    n = scheduler.run_one_report(_s(), report_id)
    flash(f"Sent report to {n} recipient(s).", "success")
    return redirect(url_for("designer.scheduled_jobs"))
