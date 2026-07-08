"""Export and import the whole metadata model as JSON.

Export captures the schema/UI definitions (tables, fields, relations, forms,
menus) — not row data and not users. Import recreates both the metadata rows and
the real physical tables/relations (reusing :mod:`app.metadata.schema_service`),
remapping every cross-reference from old ids to the new ids it assigns.

File/image fields are virtual (no physical column); their definitions round-trip
here, but the uploaded files themselves are not part of the export.
"""
import hashlib
import json
import secrets

from sqlalchemy import delete, select, text, update

from .db import engine_for, engine_for_table
from .identifiers import junction_name, validate_identifier
from .metadata import ddl, schema_service
from .metadata.field_types import ALL_TYPES, FILE_TYPES, RELATION_TYPE
from .metadata.models import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStep,
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
    RateHit,
    Role,
    Sequence,
    SlaClock,
    SlaPolicy,
    TriggerRule,
    Webhook,
    Workflow,
)

_PERMISSION_COLS = ["id", "role", "form_id", "access"]
_ROLE_COLS = ["name", "label", "builtin"]
_FIELDPERM_COLS = ["role", "field_id", "access"]
_UNIQUE_COLS = ["table_id", "name", "field_ids"]
_SEQUENCE_COLS = ["field_id", "next"]  # auto-number counters
# Data sources export *with* the password: import must connect to recreate the
# source's tables, so the credential is needed (schema export is sensitive).
_SOURCE_COLS = ["id", "name", "driver", "host", "port", "username", "password",
                "database", "active"]

SCHEMA_VERSION = 1

_TABLE_COLS = ["id", "phys_name", "label", "description", "display_field_id",
               "track_audit", "soft_delete", "row_owned", "managed", "source_id", "pk_col"]
_FIELD_COLS = ["id", "table_id", "phys_name", "label", "data_type", "length", "precision",
               "scale", "nullable", "default_value", "is_unique", "position", "enum_options",
               "related_table_id", "on_delete",
               "min_length", "max_length", "min_value", "max_value", "pattern",
               "formula", "result_type", "enum_colors"]
_RELATION_COLS = ["id", "name", "kind", "from_table_id", "to_table_id", "from_field_id",
                  "junction_phys_name", "on_delete", "to_display_field_ids",
                  "from_display_field_ids"]
_FORM_COLS = ["id", "table_id", "name", "title", "description", "purpose",
              "in_catalog", "catalog_group",
              "default_sort", "default_order", "default_per_page"]
_FORMFIELD_COLS = ["id", "form_id", "kind", "field_id", "relation_id", "label_override",
                   "widget", "required", "readonly", "help_text", "position",
                   "parent_field_id", "filter_field_id"]
_MENU_COLS = ["id", "parent_id", "label", "kind", "target_form_id", "target_table_id",
              "target_dashboard_id", "position", "icon"]
# Shared dashboards (owner_user_id NULL) + their widgets. Personal dashboards are
# per-user state (like ReportDef/SavedView) and are not exported.
_DASHBOARD_COLS = ["id", "name", "description", "columns", "position"]
_WIDGET_COLS = ["id", "dashboard_id", "title", "kind", "table_id", "query", "chart_type",
                "content", "width", "limit", "position"]
_WORKFLOW_COLS = ["id", "table_id", "field_id", "initial_state", "transitions", "layout"]
_APPROVAL_STEP_COLS = ["id", "workflow_id", "from_state", "to_state", "position", "name",
                       "approver_role", "approver_user_id"]
_TRIGGER_COLS = ["id", "table_id", "name", "active", "event", "field_id", "from_state",
                 "to_state", "cond_field_id", "cond_op", "cond_value", "in_app",
                 "notify_target", "notify_user_id", "message", "email_to", "email_subject",
                 "email_body", "webhook_url", "set_field_id", "set_value", "schedule_minutes",
                 "create_table_id", "create_field_map", "webhook_format"]
_SLA_POLICY_COLS = ["id", "table_id", "name", "active", "target_minutes", "warn_minutes",
                    "status_field_id", "start_on_create", "start_states", "pause_states",
                    "stop_states", "cond_field_id", "cond_op", "cond_value", "state_field_id",
                    "due_field_id", "breach_in_app", "breach_notify_target",
                    "breach_notify_user_id", "breach_message", "breach_email_to",
                    "breach_email_subject", "breach_email_body", "breach_set_field_id",
                    "breach_set_value", "escalations"]
# fields on SlaPolicy that reference a MetaField id and must be remapped via fmap
_SLA_FIELD_REFS = ("status_field_id", "cond_field_id", "state_field_id", "due_field_id",
                   "breach_set_field_id")
# Connections export without their token (a secret re-entered after import).
_CONNECTION_COLS = ["id", "name", "base_url", "active"]
_FEED_COLS = ["id", "name", "active", "source_table_id", "connection_id", "target_table",
              "mode", "match_target_field", "field_map", "event", "field_id", "from_state",
              "to_state", "cond_field_id", "cond_op", "cond_value", "schedule_minutes",
              "allow_manual", "skip_api_writes"]
# Webhooks export without their token_hash/secret (a fresh token is minted on
# import; the designer rotates to obtain a usable receive URL).
_WEBHOOK_COLS = ["id", "name", "active", "target_table_id", "mode", "match_field",
                 "field_map", "user_id", "max_body_bytes", "rate_limit", "rate_window"]
# Pull sources export without their headers (a secret) and without the runtime
# watermark/last_* (reset on import — like a feed's watermark).
_PULLSOURCE_COLS = ["id", "name", "active", "target_table_id", "kind", "connection_id",
                    "remote_table", "url", "records_path", "config", "field_map", "mode",
                    "match_field", "cursor_field", "page_size", "schedule_minutes", "user_id"]


class SchemaError(Exception):
    """Raised for an invalid or conflicting schema import."""


def _dump(obj, cols):
    return {c: getattr(obj, c) for c in cols}


def export_schema(session):
    """Serialise the whole model to a JSON-able dict."""
    return {
        "version": SCHEMA_VERSION,
        "tables": [_dump(t, _TABLE_COLS)
                   for t in session.scalars(select(MetaTable).order_by(MetaTable.id))],
        "fields": [_dump(f, _FIELD_COLS)
                   for f in session.scalars(select(MetaField).order_by(MetaField.id))],
        "relations": [_dump(r, _RELATION_COLS)
                      for r in session.scalars(select(MetaRelation).order_by(MetaRelation.id))],
        "forms": [_dump(f, _FORM_COLS)
                  for f in session.scalars(select(MetaForm).order_by(MetaForm.id))],
        "form_fields": [_dump(i, _FORMFIELD_COLS)
                        for i in session.scalars(select(MetaFormField).order_by(MetaFormField.id))],
        "menus": [_dump(m, _MENU_COLS)
                  for m in session.scalars(select(MetaMenu).order_by(MetaMenu.id))],
        "permissions": [_dump(p, _PERMISSION_COLS)
                        for p in session.scalars(select(MetaPermission).order_by(MetaPermission.id))],
        "workflows": [_dump(w, _WORKFLOW_COLS)
                      for w in session.scalars(select(Workflow).order_by(Workflow.id))],
        "trigger_rules": [_dump(tr, _TRIGGER_COLS)
                          for tr in session.scalars(select(TriggerRule).order_by(TriggerRule.id))],
        "sla_policies": [_dump(p, _SLA_POLICY_COLS)
                         for p in session.scalars(select(SlaPolicy).order_by(SlaPolicy.id))],
        "approval_steps": [_dump(s, _APPROVAL_STEP_COLS)
                           for s in session.scalars(select(ApprovalStep).order_by(ApprovalStep.id))],
        "roles": [_dump(r, _ROLE_COLS)
                  for r in session.scalars(select(Role).order_by(Role.id))],
        "field_permissions": [_dump(p, _FIELDPERM_COLS)
                              for p in session.scalars(select(MetaFieldPermission)
                                                       .order_by(MetaFieldPermission.id))],
        "composite_uniques": [_dump(u, _UNIQUE_COLS)
                              for u in session.scalars(select(CompositeUnique)
                                                       .order_by(CompositeUnique.id))],
        "connections": [_dump(c, _CONNECTION_COLS)
                        for c in session.scalars(select(Connection).order_by(Connection.id))],
        "feeds": [_dump(f, _FEED_COLS)
                  for f in session.scalars(select(Feed).order_by(Feed.id))],
        "webhooks": [_dump(w, _WEBHOOK_COLS)
                     for w in session.scalars(select(Webhook).order_by(Webhook.id))],
        "pull_sources": [_dump(p, _PULLSOURCE_COLS)
                         for p in session.scalars(select(PullSource).order_by(PullSource.id))],
        "dashboards": [_dump(d, _DASHBOARD_COLS)
                       for d in session.scalars(select(Dashboard)
                                                .where(Dashboard.owner_user_id.is_(None))
                                                .order_by(Dashboard.id))],
        "dashboard_widgets": [_dump(w, _WIDGET_COLS)
                              for w in session.scalars(
                                  select(DashboardWidget).join(Dashboard)
                                  .where(Dashboard.owner_user_id.is_(None))
                                  .order_by(DashboardWidget.id))],
        "sequences": [_dump(s, _SEQUENCE_COLS)
                      for s in session.scalars(select(Sequence).order_by(Sequence.id))],
        "data_sources": [_dump(d, _SOURCE_COLS)
                         for d in session.scalars(select(DataSource).order_by(DataSource.id))],
    }


# --------------------------------------------------------------------------- #
# Import
# --------------------------------------------------------------------------- #
def _make_field(f, tmap):
    return MetaField(
        table_id=tmap[f["table_id"]], phys_name=f["phys_name"], label=f["label"],
        data_type=f["data_type"], length=f.get("length"), precision=f.get("precision"),
        scale=f.get("scale"), nullable=f.get("nullable", True),
        default_value=f.get("default_value"), is_unique=f.get("is_unique", False),
        position=f.get("position", 0), enum_options=f.get("enum_options"),
        related_table_id=(tmap.get(f["related_table_id"]) if f.get("related_table_id") else None),
        on_delete=f.get("on_delete"),
        min_length=f.get("min_length"), max_length=f.get("max_length"),
        min_value=f.get("min_value"), max_value=f.get("max_value"), pattern=f.get("pattern"),
        formula=f.get("formula"), result_type=f.get("result_type"),
        enum_colors=f.get("enum_colors"),
    )


def _remap_ids_json(json_text, fmap):
    if not json_text:
        return None
    try:
        ids = json.loads(json_text)
    except (ValueError, TypeError):
        return None
    new = [fmap[i] for i in ids if i in fmap]
    return json.dumps(new) if new else None


def _drop_physical(engine, names):
    q = engine.dialect.identifier_preparer.quote
    try:
        with engine.begin() as conn:
            with ddl.fk_disabled(conn):
                for n in names:
                    conn.execute(text(f"DROP TABLE IF EXISTS {q(n)}"))
    except Exception:  # noqa: BLE001 - best effort cleanup
        pass


def wipe_model(session, engine):
    """Drop Biggy-created user/junction tables and delete every metadata row.

    External (adopted, ``managed=False``) tables are never dropped — only their
    metadata rows are removed.
    """
    # group physical drops by their source engine (resolved before metadata is deleted)
    drops = {}

    def _add(eng, name):
        drops.setdefault(id(eng), (eng, []))[1].append(name)

    for t in session.scalars(select(MetaTable)):
        if t.managed:
            _add(engine_for_table(t), t.phys_name)
    for r in session.scalars(select(MetaRelation).where(MetaRelation.kind == "mn")):
        if r.junction_phys_name:
            ft = session.get(MetaTable, r.from_table_id)
            _add(engine_for_table(ft) if ft else engine, r.junction_phys_name)
    for eng, names in drops.values():
        _drop_physical(eng, names)
    session.execute(update(MetaMenu).values(parent_id=None))
    for model in (RateHit, ApprovalAction, ApprovalRequest, ApprovalStep, SlaClock, SlaPolicy,
                  TriggerRule, Feed, Webhook, PullSource, Connection, DashboardWidget, Dashboard,
                  MetaFieldPermission, CompositeUnique, Sequence, Workflow, MetaPermission,
                  MetaMenu, MetaRelation, MetaFormField, MetaForm, MetaField, MetaTable, DataSource):
        session.execute(delete(model))
    session.commit()


def import_schema(session, engine, data, replace=False):
    """Recreate the model from an exported dict. Returns a counts summary."""
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        raise SchemaError("Unsupported or missing schema version.")

    tables = data.get("tables", [])
    fields = data.get("fields", [])
    relations = data.get("relations", [])
    forms = data.get("forms", [])
    form_fields = data.get("form_fields", [])
    menus = data.get("menus", [])
    permissions = data.get("permissions", [])

    # never trust the file: validate identifiers and types up front
    for t in tables:
        validate_identifier(t["phys_name"], kind="Table")
    for f in fields:
        validate_identifier(f["phys_name"], kind="Column")
        if f["data_type"] not in ALL_TYPES:
            raise SchemaError(f"Unknown field type: {f['data_type']}")

    if session.scalar(select(MetaTable.id).limit(1)):
        if not replace:
            raise SchemaError(
                "The target database already contains a model. "
                "Tick 'Replace existing' to wipe it and import."
            )
        wipe_model(session, engine)

    phys_by_old = {t["id"]: t["phys_name"] for t in tables}
    field_by_old = {f["id"]: f for f in fields}
    managed_by_old = {t["id"]: bool(t.get("managed", True)) for t in tables}
    source_by_old = {t["id"]: t.get("source_id") for t in tables}
    pk_by_old = {t["id"]: (t.get("pk_col") or "id") for t in tables}
    # spec of a custom (non-"id") primary-key field, per old table id
    pk_field_by_old = {f["table_id"]: f for f in fields
                       if f["phys_name"] == pk_by_old.get(f["table_id"]) != "id"}
    tmap, fmap, rmap, formmap, menumap = {}, {}, {}, {}, {}
    created_phys = []                       # list of (engine, phys_name) for rollback

    try:
        # 0. data sources (so tables resolve to the right engine).
        source_map = {}
        for d in data.get("data_sources", []):
            ds = DataSource(name=d["name"], driver=d.get("driver", "mysql+pymysql"),
                            host=d.get("host"), port=d.get("port"),
                            username=d.get("username"), password=d.get("password"),
                            database=d.get("database"), active=d.get("active", True))
            session.add(ds)
            session.flush()
            source_map[d["id"]] = ds

        def _teng(old_sid):
            return engine_for(source_map.get(old_sid) if old_sid else None)

        # 1. tables (physical + metadata) in their own source; external
        # (managed=False) tables are assumed to already exist — no DDL.
        for t in tables:
            managed = bool(t.get("managed", True))
            ds = source_map.get(t.get("source_id"))
            mt = MetaTable(phys_name=t["phys_name"], label=t["label"],
                           description=t.get("description"),
                           track_audit=bool(t.get("track_audit")),
                           soft_delete=bool(t.get("soft_delete")),
                           row_owned=bool(t.get("row_owned")), managed=managed,
                           source_id=ds.id if ds else None,
                           pk_col=t.get("pk_col") or "id")
            session.add(mt)
            session.flush()
            tmap[t["id"]] = mt.id
            if managed:
                teng = _teng(t.get("source_id"))
                pk_spec = pk_field_by_old.get(t["id"])     # custom/natural PK column
                pk_field = _make_field(pk_spec, tmap) if pk_spec else None
                schema_service.create_physical_table(teng, mt.phys_name, [], pk=pk_field)
                schema_service.ensure_record_columns(teng, mt)
                created_phys.append((teng, mt.phys_name))

        # 2. scalar fields (+ physical columns). Relations are created in step 3;
        # file/image fields are virtual; external-table columns already exist.
        for f in fields:
            if f["data_type"] == RELATION_TYPE:
                continue
            mf = _make_field(f, tmap)
            session.add(mf)
            session.flush()
            fmap[f["id"]] = mf.id
            is_pk = f["phys_name"] == pk_by_old.get(f["table_id"]) != "id"   # already the PK column
            if (f["data_type"] not in FILE_TYPES and not is_pk
                    and managed_by_old.get(f["table_id"], True)):
                schema_service.add_scalar_column(
                    _teng(source_by_old.get(f["table_id"])), phys_by_old[f["table_id"]], mf)

        # 3. relations (m1 creates its FK field + column; mn creates a junction)
        for r in relations:
            if r["kind"] == "m1":
                of = field_by_old.get(r["from_field_id"])
                if not of:
                    continue
                mf = _make_field(of, tmap)
                session.add(mf)
                session.flush()
                fmap[of["id"]] = mf.id
                if managed_by_old.get(of["table_id"], True):   # external FK col exists
                    schema_service.add_relation_column(
                        _teng(source_by_old.get(of["table_id"])),
                        phys_by_old[of["table_id"]], mf,
                        phys_by_old[of["related_table_id"]],
                    )
                rel = MetaRelation(
                    name=r["name"], kind="m1", from_table_id=tmap[r["from_table_id"]],
                    to_table_id=tmap[r["to_table_id"]], from_field_id=mf.id,
                    on_delete=r.get("on_delete"),
                )
            else:  # mn
                a_phys = phys_by_old[r["from_table_id"]]
                b_phys = phys_by_old[r["to_table_id"]]
                jname = junction_name(a_phys, b_phys)
                left_col, right_col = f"{a_phys}_id", f"{b_phys}_id"
                if left_col == right_col:
                    right_col = f"{b_phys}_id_2"
                jeng = _teng(source_by_old.get(r["from_table_id"]))
                if not schema_service.table_exists(jeng, jname):
                    schema_service.create_junction_table(
                        jeng, jname, a_phys, left_col, b_phys, right_col
                    )
                    created_phys.append((jeng, jname))
                rel = MetaRelation(
                    name=r["name"], kind="mn", from_table_id=tmap[r["from_table_id"]],
                    to_table_id=tmap[r["to_table_id"]], junction_phys_name=jname,
                )
            session.add(rel)
            session.flush()
            rmap[r["id"]] = rel.id
            rel.to_display_field_ids = _remap_ids_json(r.get("to_display_field_ids"), fmap)
            rel.from_display_field_ids = _remap_ids_json(r.get("from_display_field_ids"), fmap)

        # 4. deferred table display field
        for t in tables:
            old_disp = t.get("display_field_id")
            if old_disp and old_disp in fmap:
                session.get(MetaTable, tmap[t["id"]]).display_field_id = fmap[old_disp]

        # 5. forms + items
        for fm in forms:
            mform = MetaForm(table_id=tmap[fm["table_id"]], name=fm["name"],
                             title=fm["title"], description=fm.get("description"),
                             purpose=fm.get("purpose", "data"),
                             in_catalog=fm.get("in_catalog", False),
                             catalog_group=fm.get("catalog_group"),
                             default_sort=fm.get("default_sort"),
                             default_order=fm.get("default_order"),
                             default_per_page=fm.get("default_per_page"))
            session.add(mform)
            session.flush()
            formmap[fm["id"]] = mform.id
        for it in form_fields:
            form_id = formmap.get(it["form_id"])
            if not form_id:
                continue
            field_id = fmap.get(it["field_id"]) if it.get("field_id") else None
            relation_id = rmap.get(it["relation_id"]) if it.get("relation_id") else None
            if it["kind"] == "field" and not field_id:
                continue
            if it["kind"] == "relation" and not relation_id:
                continue
            session.add(MetaFormField(
                form_id=form_id, kind=it["kind"], field_id=field_id, relation_id=relation_id,
                label_override=it.get("label_override"), widget=it.get("widget"),
                required=it.get("required", False), readonly=it.get("readonly", False),
                help_text=it.get("help_text"), position=it.get("position", 0),
                parent_field_id=(fmap.get(it["parent_field_id"]) if it.get("parent_field_id") else None),
                filter_field_id=(fmap.get(it["filter_field_id"]) if it.get("filter_field_id") else None),
            ))

        # 5c. shared dashboards + their widgets (before menus, so the menu remap works)
        dashmap = {}
        for d in data.get("dashboards", []):
            dash = Dashboard(name=d["name"], description=d.get("description"),
                             owner_user_id=None, columns=d.get("columns", 2),
                             position=d.get("position", 0))
            session.add(dash)
            session.flush()
            dashmap[d["id"]] = dash.id
        for w in data.get("dashboard_widgets", []):
            did = dashmap.get(w.get("dashboard_id"))
            if not did:
                continue
            session.add(DashboardWidget(
                dashboard_id=did, title=w.get("title"), kind=w.get("kind", "chart"),
                table_id=(tmap.get(w["table_id"]) if w.get("table_id") else None),
                query=w.get("query"), chart_type=w.get("chart_type", "bar"),
                content=w.get("content"), width=w.get("width", 1), limit=w.get("limit", 5),
                position=w.get("position", 0)))

        # 6. menus (create, then wire parents)
        for m in menus:
            mm = MetaMenu(
                label=m["label"], kind=m["kind"],
                target_form_id=(formmap.get(m["target_form_id"]) if m.get("target_form_id") else None),
                target_table_id=(tmap.get(m["target_table_id"]) if m.get("target_table_id") else None),
                target_dashboard_id=(dashmap.get(m["target_dashboard_id"])
                                     if m.get("target_dashboard_id") else None),
                position=m.get("position", 0), icon=m.get("icon"),
            )
            session.add(mm)
            session.flush()
            menumap[m["id"]] = mm.id
        for m in menus:
            if m.get("parent_id") and m["parent_id"] in menumap:
                session.get(MetaMenu, menumap[m["id"]]).parent_id = menumap[m["parent_id"]]

        # 7. permissions (remap form_id)
        for p in permissions:
            form_id = formmap.get(p.get("form_id"))
            if form_id:
                session.add(MetaPermission(role=p["role"], form_id=form_id,
                                           access=p.get("access", "write")))

        # 8. workflows (remap table_id + field_id; keep old→new id for approval steps)
        wfmap = {}
        for w in data.get("workflows", []):
            table_id = tmap.get(w.get("table_id"))
            field_id = fmap.get(w.get("field_id"))
            if table_id and field_id:
                wf = Workflow(table_id=table_id, field_id=field_id,
                              initial_state=w.get("initial_state"),
                              transitions=w.get("transitions"), layout=w.get("layout"))
                session.add(wf)
                session.flush()
                if w.get("id") is not None:
                    wfmap[w["id"]] = wf.id

        # 9. trigger rules (remap table + field references; users aren't in schema)
        for tr in data.get("trigger_rules", []):
            table_id = tmap.get(tr.get("table_id"))
            if not table_id:
                continue
            session.add(TriggerRule(
                table_id=table_id, name=tr.get("name", "rule"),
                active=tr.get("active", True), event=tr.get("event", "update"),
                field_id=fmap.get(tr.get("field_id")), from_state=tr.get("from_state"),
                to_state=tr.get("to_state"), cond_field_id=fmap.get(tr.get("cond_field_id")),
                cond_op=tr.get("cond_op"), cond_value=tr.get("cond_value"),
                in_app=tr.get("in_app", False), notify_target=tr.get("notify_target"),
                notify_user_id=tr.get("notify_user_id"), message=tr.get("message"),
                email_to=tr.get("email_to"), email_subject=tr.get("email_subject"),
                email_body=tr.get("email_body"), webhook_url=tr.get("webhook_url"),
                set_field_id=fmap.get(tr.get("set_field_id")), set_value=tr.get("set_value"),
                schedule_minutes=tr.get("schedule_minutes"),
                create_table_id=tmap.get(tr.get("create_table_id")),
                create_field_map=tr.get("create_field_map"),
                webhook_format=tr.get("webhook_format")))

        # 9b. SLA policies (remap table + every field reference; clocks are runtime, not imported)
        for sp in data.get("sla_policies", []):
            table_id = tmap.get(sp.get("table_id"))
            if not table_id:
                continue
            kwargs = {c: sp.get(c) for c in _SLA_POLICY_COLS if c not in ("id", "table_id")}
            for ref in _SLA_FIELD_REFS:
                kwargs[ref] = fmap.get(sp.get(ref))
            kwargs.setdefault("target_minutes", 60)
            session.add(SlaPolicy(table_id=table_id, **kwargs))

        # 9c. approval steps (remap workflow_id; users aren't in schema → keep id as-is)
        for sp in data.get("approval_steps", []):
            wid = wfmap.get(sp.get("workflow_id"))
            if not wid or not sp.get("from_state") or not sp.get("to_state"):
                continue
            session.add(ApprovalStep(
                workflow_id=wid, from_state=sp["from_state"], to_state=sp["to_state"],
                position=sp.get("position", 1), name=sp.get("name"),
                approver_role=sp.get("approver_role"),
                approver_user_id=sp.get("approver_user_id")))

        # 10. roles (upsert by name) + field permissions (remap field_id)
        have_roles = {r.name for r in session.scalars(select(Role))}
        for r in data.get("roles", []):
            if r.get("name") and r["name"] not in have_roles:
                session.add(Role(name=r["name"], label=r.get("label", r["name"]),
                                 builtin=r.get("builtin", False)))
                have_roles.add(r["name"])
        for fp in data.get("field_permissions", []):
            field_id = fmap.get(fp.get("field_id"))
            if field_id and fp.get("role"):
                session.add(MetaFieldPermission(role=fp["role"], field_id=field_id,
                                                access=fp.get("access", "write")))

        # 11. composite unique constraints (re-apply the ALTER + record metadata)
        for cu in data.get("composite_uniques", []):
            tid = tmap.get(cu.get("table_id"))
            old_fids = json.loads(cu.get("field_ids") or "[]")
            cols = [field_by_old[i]["phys_name"] for i in old_fids if i in field_by_old]
            new_fids = [fmap[i] for i in old_fids if i in fmap]
            if not tid or len(cols) < 2:
                continue
            try:
                schema_service.add_composite_unique(
                    _teng(source_by_old.get(cu["table_id"])),
                    phys_by_old[cu["table_id"]], cu["name"], cols)
            except Exception:  # noqa: BLE001
                continue
            session.add(CompositeUnique(table_id=tid, name=cu["name"],
                                        field_ids=json.dumps(new_fids)))

        # 12. connections (token redacted — re-entered after import) + feeds
        cmap = {}
        for c in data.get("connections", []):
            conn = Connection(name=c["name"], base_url=c["base_url"],
                              active=c.get("active", True), token=None)
            session.add(conn)
            session.flush()
            cmap[c["id"]] = conn.id
        for fd in data.get("feeds", []):
            stid = tmap.get(fd.get("source_table_id"))
            cid = cmap.get(fd.get("connection_id"))
            if not stid or not cid:
                continue
            session.add(Feed(
                name=fd["name"], active=fd.get("active", True), source_table_id=stid,
                connection_id=cid, target_table=fd["target_table"],
                mode=fd.get("mode", "create"), match_target_field=fd.get("match_target_field"),
                field_map=fd.get("field_map"), event=fd.get("event"),
                field_id=fmap.get(fd.get("field_id")), from_state=fd.get("from_state"),
                to_state=fd.get("to_state"), cond_field_id=fmap.get(fd.get("cond_field_id")),
                cond_op=fd.get("cond_op"), cond_value=fd.get("cond_value"),
                schedule_minutes=fd.get("schedule_minutes"),
                allow_manual=fd.get("allow_manual", True),
                skip_api_writes=fd.get("skip_api_writes", True)))

        # 12b. inbound webhooks (token/secret redacted — a fresh token is minted;
        # rotate in the UI to get a usable receive URL).
        for wb in data.get("webhooks", []):
            ttid = tmap.get(wb.get("target_table_id"))
            if not ttid:
                continue
            raw = "whk_" + secrets.token_urlsafe(32)
            session.add(Webhook(
                name=wb["name"], active=wb.get("active", True), target_table_id=ttid,
                token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(), prefix=raw[:12],
                mode=wb.get("mode", "create"), match_field=wb.get("match_field"),
                field_map=wb.get("field_map"), user_id=wb.get("user_id"),
                max_body_bytes=wb.get("max_body_bytes"), rate_limit=wb.get("rate_limit"),
                rate_window=wb.get("rate_window")))

        # 12c. pull sources (headers redacted — re-entered after import; watermark reset).
        for ps in data.get("pull_sources", []):
            ttid = tmap.get(ps.get("target_table_id"))
            if not ttid:
                continue
            session.add(PullSource(
                name=ps["name"], active=ps.get("active", True), target_table_id=ttid,
                kind=ps.get("kind", "peer"), connection_id=cmap.get(ps.get("connection_id")),
                remote_table=ps.get("remote_table"), url=ps.get("url"),
                records_path=ps.get("records_path"), config=ps.get("config"),
                field_map=ps.get("field_map"),
                mode=ps.get("mode", "upsert"), match_field=ps.get("match_field"),
                cursor_field=ps.get("cursor_field"), page_size=ps.get("page_size"),
                schedule_minutes=ps.get("schedule_minutes"), user_id=ps.get("user_id")))

        # 13. auto-number counters (remap field_id) so numbering continues
        for sq in data.get("sequences", []):
            fid = fmap.get(sq.get("field_id"))
            if fid:
                session.add(Sequence(field_id=fid, next=sq.get("next", 1)))

        session.commit()
    except Exception:
        session.rollback()
        for eng, name in created_phys:     # each in the source it was created in
            _drop_physical(eng, [name])
        raise

    return {"tables": len(tmap), "fields": len(fmap), "relations": len(rmap),
            "forms": len(formmap), "menus": len(menumap)}
