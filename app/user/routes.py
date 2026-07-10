"""User mode: menu-driven list/search, CRUD, trash, related records."""
import calendar as _calendar
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode

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
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, select
from werkzeug.datastructures import MultiDict

from .. import (
    approvals,
    comments,
    dashboards,
    data_service,
    feeds,
    file_store,
    helpers,
    importer,
    list_export,
    maintenance,
    record_service,
    reporting,
    sla,
    topology,
    watch,
    workflow,
)
from .. import filters as filt
from ..api import tokens as api_tokens
from ..db import SessionLocal, engine_for_table
from ..forms.admin_forms import ImportForm
from ..forms.builder import build_form, display_field_name, m1_target_and_columns
from ..helpers import can_read, can_write, current_user_id, form_access, table_view_form
from ..metadata.field_types import RELATION_TYPE, is_text_search, type_label
from ..metadata.models import (
    ApiToken,
    ApprovalRequest,
    AppUser,
    Attachment,
    AuditLog,
    Dashboard,
    DashboardWidget,
    Feed,
    MetaField,
    MetaForm,
    MetaRelation,
    MetaTable,
    Notification,
    ReportDef,
    SavedView,
)

bp = Blueprint("user", __name__, url_prefix="/u")

PER_PAGE = 25
ALLOWED_PER_PAGE = (25, 50, 100)


@dataclass
class ListQuery:
    """Everything a list view derives from the request (filters/sort/paging)."""
    built: object
    columns: list
    filter_meta: dict
    filter_order: list
    label_maps: dict
    m1_targets: dict
    q: str
    page: int
    per_page: int
    sort: object
    order: str
    conditions: list
    filters: list


@bp.before_request
@login_required
def _guard():
    if current_user.is_portal:               # customers live in /portal only
        return redirect(url_for("portal.home"))


def _s():
    return SessionLocal()


def _ctx():
    return current_user_id(), current_user.is_designer


def _get_form(session, form_id):
    mf = session.get(MetaForm, form_id)
    if not mf:
        abort(404)
    return mf


def _require(form_id, level):
    """Load the form and enforce 'read'/'write' access. Returns (mf, access)."""
    session = _s()
    mf = _get_form(session, form_id)
    access = form_access(session, current_user, form_id)
    ok = can_write(access) if level == "write" else can_read(access)
    if not ok:
        abort(403)
    return mf, access


def _safe_next(default):
    nxt = request.args.get("next") or request.form.get("next")
    if nxt and nxt.startswith("/") and not nxt.startswith("//"):
        return nxt
    return default


def _recent_records(session, user_id, limit=8):
    """The user's most recently created/updated records, from the audit log.

    Only tables the user can read; rows that no longer resolve (deleted,
    table dropped, source down) are skipped.
    """
    tables = {t.phys_name: t for t in session.scalars(select(MetaTable))}
    seen, out = set(), []
    logs = session.scalars(
        select(AuditLog)
        .where(AuditLog.user_id == user_id, AuditLog.action.in_(("create", "update")))
        .order_by(AuditLog.id.desc()).limit(60))
    for log in logs:
        key = (log.table_phys, str(log.row_pk))
        if log.row_pk is None or key in seen:
            continue
        seen.add(key)
        table = tables.get(log.table_phys)
        if not table or not helpers.table_readable(session, current_user, table):
            continue
        try:
            row = data_service.get_row(engine_for_table(table), table.phys_name, log.row_pk)
        except Exception:  # noqa: BLE001 - a broken source must not break the home page
            continue
        if not row or row.get("deleted_at"):
            continue
        label = row.get(display_field_name(session, table))
        out.append({"table": table, "pk": log.row_pk,
                    "label": label if label not in (None, "") else f"#{log.row_pk}",
                    "at": log.at})
        if len(out) >= limit:
            break
    return out


def _my_work(session, user_id, is_designer, limit=12):
    """Open items assigned to the caller: rows whose (first) user field == me."""
    out = []
    for t in session.scalars(select(MetaTable)):
        uf = next((f for f in t.fields if f.data_type == "user"), None)
        if uf is None or not helpers.table_readable(session, current_user, t):
            continue
        try:
            rows, _total = record_service.list_records(
                engine_for_table(t), t, user_id=user_id, is_designer=is_designer,
                filters=[{"col": uf.phys_name, "op": "eq", "value": user_id}],
                sort="updated_at" if t.track_audit else None, order="desc",
                per_page=limit)
        except Exception:  # noqa: BLE001 - a broken source must not break home
            continue
        disp = display_field_name(session, t)
        status_f = next((f for f in t.fields if f.data_type == "enum"), None)
        colors = json.loads(status_f.enum_colors) \
            if status_f is not None and status_f.enum_colors else None
        for r in rows:
            out.append({"table": t, "pk": r[t.pk_col],
                        "label": str(r.get(disp)) if r.get(disp) not in (None, "")
                        else f"#{r[t.pk_col]}",
                        "status": r.get(status_f.phys_name) if status_f else None,
                        "colors": colors})
    return out[:limit]


def _sla_next(session, limit=8):
    """Soonest-due SLA clocks with resolved record labels (home panel)."""
    out = []
    for e in sla.breaching_next(session, current_user, limit=limit):
        try:
            row = data_service.get_row(engine_for_table(e["table"]),
                                       e["table"].phys_name, e["pk"])
        except Exception:  # noqa: BLE001 - a broken source must not break home
            row = None
        if not row or row.get("deleted_at"):
            continue
        label = row.get(display_field_name(session, e["table"]))
        e["label"] = str(label) if label not in (None, "") else f"#{e['pk']}"
        out.append(e)
    return out


@bp.route("/")
def dashboard():
    session = _s()
    has_forms = session.scalar(select(MetaForm.id).limit(1)) is not None
    user_id, is_designer = _ctx()
    tiles = []
    pinned = session.scalars(
        select(ReportDef).where(ReportDef.user_id == user_id, ReportDef.pinned.is_(True))
        .order_by(ReportDef.name)).all()
    for r in pinned:
        table = session.get(MetaTable, r.table_id)
        if not table or not helpers.table_readable(session, current_user, table):
            continue
        args = MultiDict(parse_qsl(r.query or ""))
        scope = record_service._scope_filters(
            table, user_id=user_id, is_designer=is_designer, include_deleted=False)
        ctx = reporting.build(session, engine_for_table(table), table, args,
                              base_filters=scope, user=current_user)
        tiles.append({"report": r, "table": table,
                      "chart": ctx["chart"], "chart_data": ctx["chart_data"]})
    personal = session.scalars(
        select(Dashboard).where(Dashboard.owner_user_id == user_id)
        .order_by(Dashboard.position, Dashboard.id)).all()
    shared = [d for d in session.scalars(
        select(Dashboard).where(Dashboard.owner_user_id.is_(None)).order_by(Dashboard.name))
        if dashboards.visible(session, current_user, d)]
    return render_template("user/dashboard.html", has_forms=has_forms, tiles=tiles,
                           personal=personal, shared=shared,
                           my_work=_my_work(session, user_id, is_designer),
                           sla_next=_sla_next(session),
                           recent=_recent_records(session, user_id))


# --------------------------------------------------------------------------- #
# Dashboards (view any visible; manage personal ones)
# --------------------------------------------------------------------------- #
@bp.route("/dashboards/<int:dash_id>")
def dashboard_view(dash_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    if not dash or not dashboards.visible(session, current_user, dash):
        abort(404)
    tiles = dashboards.render(session, current_user, dash)
    user_tables = [t for t in session.scalars(select(MetaTable).order_by(MetaTable.label))
                   if helpers.table_readable(session, current_user, t)]
    return render_template("user/dashboard_view.html", dash=dash, tiles=tiles,
                           user_tables=user_tables)


@bp.route("/dashboards")
def my_dashboards():
    session = _s()
    items = session.scalars(
        select(Dashboard).where(Dashboard.owner_user_id == current_user_id())
        .order_by(Dashboard.position, Dashboard.id)).all()
    return render_template("user/my_dashboards.html", items=items)


@bp.route("/dashboards/new", methods=["POST"])
def my_dashboard_create():
    session = _s()
    name = (request.form.get("name") or "").strip()
    if name:
        dash = Dashboard(name=name, owner_user_id=current_user_id())
        session.add(dash)
        session.commit()
        return redirect(url_for("user.dashboard_view", dash_id=dash.id))
    return redirect(url_for("user.my_dashboards"))


@bp.route("/dashboards/<int:dash_id>/delete", methods=["POST"])
def my_dashboard_delete(dash_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    if dash and dash.owner_user_id == current_user_id():
        session.delete(dash)
        session.commit()
        flash("Dashboard deleted.", "info")
    return redirect(url_for("user.my_dashboards"))


@bp.route("/dashboards/<int:dash_id>/widgets", methods=["POST"])
def my_widget_add(dash_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    if not dash or dash.owner_user_id != current_user_id():
        abort(404)
    pos = max([x.position for x in dash.widgets], default=-1) + 1
    session.add(DashboardWidget(
        dashboard_id=dash.id, position=pos, kind=request.form.get("kind") or "chart",
        title=(request.form.get("title") or "").strip() or None,
        table_id=request.form.get("table_id", type=int) or None,
        query=request.form.get("query") or None,
        chart_type=request.form.get("chart_type") or "bar",
        content=request.form.get("content") or None,
        width=request.form.get("width", type=int) or 1,
        limit=request.form.get("limit", type=int) or 5))
    session.commit()
    flash("Widget added.", "success")
    return redirect(url_for("user.dashboard_view", dash_id=dash_id))


@bp.route("/dashboards/<int:dash_id>/widgets/<int:widget_id>/delete", methods=["POST"])
def my_widget_delete(dash_id, widget_id):
    session = _s()
    dash = session.get(Dashboard, dash_id)
    w = session.get(DashboardWidget, widget_id)
    if dash and dash.owner_user_id == current_user_id() and w and w.dashboard_id == dash.id:
        session.delete(w)
        session.commit()
        flash("Widget removed.", "info")
    return redirect(url_for("user.dashboard_view", dash_id=dash_id))


@bp.route("/reports/<int:table_id>/to-dashboard", methods=["POST"])
def report_to_dashboard(table_id):
    """Add the current report (query + chart type) as a widget on a chosen dashboard."""
    session = _s()
    table = session.get(MetaTable, table_id)
    dash = session.get(Dashboard, request.form.get("dashboard_id", type=int))
    if not table or not dash:
        flash("Pick a dashboard.", "warning")
        return redirect(_safe_next(url_for("user.report", table_id=table_id)))
    # only the owner (personal) or a designer (shared) may add to it
    if dash.owner_user_id not in (None, current_user_id()) or \
            (dash.owner_user_id is None and not current_user.is_designer):
        abort(403)
    kind = "number" if request.form.get("as") == "number" else "chart"
    pos = max([x.position for x in dash.widgets], default=-1) + 1
    session.add(DashboardWidget(
        dashboard_id=dash.id, position=pos, kind=kind, table_id=table.id,
        title=(request.form.get("title") or table.label),
        query=request.form.get("query", ""), chart_type=request.form.get("chart") or "bar"))
    session.commit()
    flash(f"Added to “{dash.name}”.", "success")
    return redirect(url_for("user.dashboard_view", dash_id=dash.id))


@bp.route("/badges")
def badges():
    """Tiny JSON for live topbar badges (polled by app.js)."""
    from .. import approvals
    session = _s()
    unread = session.scalar(select(func.count()).select_from(Notification).where(
        Notification.channel == "in_app", Notification.user_id == current_user_id(),
        Notification.status == "unread")) or 0
    return jsonify(notifications=unread,
                   approvals=approvals.pending_count_for_user(session, current_user))


@bp.route("/catalog")
def catalog():
    """Service catalog: request cards for forms flagged in_catalog."""
    session = _s()
    groups = {}
    for f in session.scalars(select(MetaForm).where(MetaForm.in_catalog.is_(True))
                             .order_by(MetaForm.title)):
        if f.purpose == "view" or not can_read(form_access(session, current_user, f.id)):
            continue
        groups.setdefault(f.catalog_group or "General", []).append(f)
    return render_template("user/catalog.html", groups=dict(sorted(groups.items())))


@bp.route("/my-requests")
def my_requests():
    """The caller's submissions across catalog-backed tables (owner-stamped)."""
    session = _s()
    uid = current_user_id()
    items, seen = [], set()
    for f in session.scalars(select(MetaForm).where(MetaForm.in_catalog.is_(True))
                             .order_by(MetaForm.title)):
        t = f.table
        if t.id in seen or f.purpose == "view" \
                or not can_read(form_access(session, current_user, f.id)) \
                or not (t.track_audit or t.row_owned):   # needs created_by stamps
            continue
        seen.add(t.id)
        engine = engine_for_table(t)
        disp = display_field_name(session, t)
        status_col = next((fd.phys_name for fd in t.fields if fd.data_type == "enum"), None)
        rows, _total = record_service.list_records(
            engine, t, user_id=uid, is_designer=True,        # explicit owner filter below
            filters=[{"col": "created_by", "op": "eq", "value": uid}],
            sort="created_at", order="desc", per_page=50)
        for r in rows:
            items.append({"table": t, "pk": r[t.pk_col],
                          "label": str(r.get(disp)) if r.get(disp) not in (None, "")
                          else f"#{r[t.pk_col]}",
                          "status": r.get(status_col) if status_col else None,
                          "created_at": r.get("created_at"),
                          "viewable": table_view_form(session, t.id) is not None})
    items.sort(key=lambda x: (x["created_at"] is None, x["created_at"]), reverse=True)
    return render_template("user/my_requests.html", items=items)


def _q_filter(session, table, q):
    """An OR-group matching ``q`` against every text-like column of ``table``."""
    cols = [f.phys_name for f in table.fields if is_text_search(f.data_type)]
    if not cols:
        cols = [display_field_name(session, table)]
    return {"any": [{"col": c, "op": "contains", "value": q, "is_text": True}
                    for c in cols]}


def _match_snippet(session, table, row, q):
    """(field_label, before, match, after) for the first column containing ``q``."""
    ql = q.lower()
    disp = display_field_name(session, table)
    fields = sorted((f for f in table.fields if is_text_search(f.data_type)),
                    key=lambda f: f.phys_name != disp)     # display field first
    for f in fields:
        s = "" if row.get(f.phys_name) is None else str(row.get(f.phys_name))
        i = s.lower().find(ql)
        if i >= 0:
            start, end = max(0, i - 40), min(len(s), i + len(q) + 40)
            return (f.label, ("…" if start else "") + s[start:i], s[i:i + len(q)],
                    s[i + len(q):end] + ("…" if end < len(s) else ""))
    return None


def _first_readable_form(session, table_id):
    for f in session.scalars(select(MetaForm).where(MetaForm.table_id == table_id,
                                                    MetaForm.purpose != "view")
                             .order_by(MetaForm.id)):
        if can_read(form_access(session, current_user, f.id)):
            return f
    return None


@bp.route("/search")
def search():
    """Global search: every text-like column of every table the user can view."""
    q = (request.args.get("q") or "").strip()
    session = _s()
    user_id, is_designer = _ctx()
    groups = []
    if q:
        for table in _all_tables(session):
            vf = table_view_form(session, table.id)
            if not vf or not can_read(form_access(session, current_user, vf.id)):
                continue
            disp = display_field_name(session, table)
            rows, total = record_service.list_records(
                engine_for_table(table), table, user_id=user_id, is_designer=is_designer,
                filters=[_q_filter(session, table, q)], per_page=10)
            if not rows:
                continue
            records = [{"pk": r[table.pk_col],
                        "label": str(r.get(disp)) if r.get(disp) not in (None, "")
                        else f"#{r[table.pk_col]}",
                        "snippet": _match_snippet(session, table, r, q)} for r in rows]
            lf = _first_readable_form(session, table.id)
            groups.append({"table_id": table.id, "label": table.label,
                           "records": records, "total": total,
                           "list_url": url_for("user.form_list", form_id=lf.id, q=q)
                           if lf else None})
    return render_template("user/search.html", q=q, groups=groups)


# --------------------------------------------------------------------------- #
# API tokens (per user)
# --------------------------------------------------------------------------- #
@bp.route("/tokens", methods=["GET", "POST"])
def tokens():
    session = _s()
    uid = current_user_id()
    new_raw = None
    if request.method == "POST":
        name = (request.form.get("name") or "").strip() or "token"
        _tok, new_raw = api_tokens.mint(session, uid, name)
        flash("Token created — copy it now; it won't be shown again.", "success")
    items = session.scalars(
        select(ApiToken).where(ApiToken.user_id == uid).order_by(ApiToken.id.desc())).all()
    return render_template("user/tokens.html", tokens=items, new_raw=new_raw)


@bp.route("/notifications")
def notifications():
    session = _s()
    uid = current_user_id()
    items = session.scalars(
        select(Notification).where(Notification.channel == "in_app", Notification.user_id == uid)
        .order_by(Notification.id.desc()).limit(100)).all()
    table_ids = {t.phys_name: t.id for t in session.scalars(select(MetaTable))}
    return render_template("user/notifications.html", items=items, table_ids=table_ids)


@bp.route("/notifications/read", methods=["POST"])
def notifications_read():
    session = _s()
    one = request.form.get("id", type=int)
    q = select(Notification).where(Notification.channel == "in_app",
                                   Notification.user_id == current_user_id(),
                                   Notification.status == "unread")
    if one:
        q = q.where(Notification.id == one)
    for n in session.scalars(q):
        n.status = "read"
    session.commit()
    return redirect(url_for("user.notifications"))


@bp.route("/approvals")
def approvals_inbox():
    session = _s()
    table_ids = {t.phys_name: t.id for t in session.scalars(select(MetaTable))}
    items = []
    for req in approvals.pending_for_user(session, current_user):
        steps = approvals.steps_for(session, req.workflow_id, req.from_state, req.to_state)
        cur = [s for s in steps if s.position == req.current_position]
        items.append({"req": req, "table_id": table_ids.get(req.table_phys),
                      "waiting": ", ".join(s.name or s.approver_role or "approver" for s in cur),
                      "step_no": req.current_position,
                      "n_positions": len(sorted({s.position for s in steps}))})
    return render_template("user/approvals.html", items=items)


@bp.route("/approvals/<int:request_id>/act", methods=["POST"])
def approval_act(request_id):
    session = _s()
    req = session.get(ApprovalRequest, request_id)
    if not req:
        abort(404)
    decision = request.form.get("decision")
    if decision not in ("approve", "reject"):
        abort(400)
    try:
        approvals.act(session, req, current_user, decision, request.form.get("comment"))
        flash(f"Request {decision}d.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(_safe_next(url_for("user.approvals_inbox")))


@bp.route("/tokens/<int:token_id>/revoke", methods=["POST"])
def token_revoke(token_id):
    session = _s()
    tok = session.get(ApiToken, token_id)
    if tok and tok.user_id == current_user_id():
        tok.revoked = True
        session.commit()
        flash("Token revoked.", "info")
    return redirect(url_for("user.tokens"))


# --------------------------------------------------------------------------- #
# List / search
# --------------------------------------------------------------------------- #
def _list_query(mf, session, engine):
    """Parse the request into a :class:`ListQuery` (shared by list/export/bulk)."""
    built = build_form(mf, session, engine, current_user)
    columns = [it for it in built.items if it.kind in ("field", "relation_m1")]
    filter_meta, filter_order, label_maps, m1_targets = filt.build_meta(
        session, engine, columns, user=current_user)

    q = request.args.get("q", "").strip()
    page = int(request.args.get("page", 1) or 1)
    try:
        per_page = int(request.args.get("per_page") or mf.default_per_page or PER_PAGE)
    except (TypeError, ValueError):
        per_page = PER_PAGE
    if per_page not in ALLOWED_PER_PAGE:
        per_page = PER_PAGE
    # designer-chosen list defaults apply when the request doesn't say otherwise
    # (a stale default_sort — renamed column — is ignored)
    default_sort = mf.default_sort if mf.default_sort in {
        f.phys_name for f in mf.table.fields} | {mf.table.pk_col} else None
    sort = request.args.get("sort") or default_sort
    order = request.args.get("order") or mf.default_order or "asc"

    filters, conditions = [], []
    if q:
        # match across every text-like column of the table (not just the display field)
        filters.append(_q_filter(session, mf.table, q))
    for i, (col, op, val) in enumerate(zip(request.args.getlist("fcol"),
                                           request.args.getlist("fop"),
                                           request.args.getlist("fval"))):
        meta = filter_meta.get(col)
        if not meta or not filt.valid_op(meta["kind"], op):
            continue
        # idx = position in the raw fcol/fop/fval lists (for chip remove-URLs)
        conditions.append({"col": col, "op": op, "val": val, "idx": i})
        # "me" token on user fields resolves per viewer — saved views stay portable
        fval = current_user_id() if meta["kind"] == "user" and val == "me" else val
        if op in filt.NO_VALUE_OPS or val != "":
            filters.append({"col": col, "op": op, "value": fval,
                            "is_text": meta["kind"] == "text"})

    return ListQuery(built, columns, filter_meta, filter_order, label_maps,
                     m1_targets, q, page, per_page, sort, order, conditions, filters)


def _filter_chips(mf, lq, all_args):
    """Removable-chip descriptions of the active quick search + conditions.

    Each chip's URL is the current list URL minus that one criterion (and minus
    the page, which may no longer exist once the filter loosens).
    """
    def url_without(*, drop_q=False, drop_idx=None):
        args = {k: list(v) for k, v in all_args.items() if k != "page"}
        if drop_q:
            args.pop("q", None)
        if drop_idx is not None:
            for key in ("fcol", "fop", "fval"):
                vals = args.get(key)
                if vals and drop_idx < len(vals):
                    del vals[drop_idx]
        return url_for("user.form_list", form_id=mf.id, **args)

    chips = []
    if lq.q:
        chips.append({"text": f'search: “{lq.q}”', "url": url_without(drop_q=True)})
    for c in lq.conditions:
        meta = lq.filter_meta[c["col"]]
        op_label = next((lbl for key, lbl, _ in meta["ops"] if key == c["op"]), c["op"])
        val = "" if c["op"] in filt.NO_VALUE_OPS else c["val"]
        if val and meta.get("choices"):  # m1/enum: show the display label, not the id
            val = next((lbl for v, lbl in meta["choices"] if str(v) == str(val)), val)
        chips.append({"text": " ".join(str(p) for p in (meta["label"], op_label, val) if p != ""),
                      "url": url_without(drop_idx=c["idx"])})
    return chips


@bp.route("/forms/<int:form_id>")
def form_list(form_id):
    mf, access = _require(form_id, "read")
    session = _s()
    engine = engine_for_table(mf.table)
    lq = _list_query(mf, session, engine)

    user_id, is_designer = _ctx()
    rows, total = record_service.list_records(
        engine, mf.table, user_id=user_id, is_designer=is_designer,
        filters=lq.filters, sort=lq.sort, order=lq.order, page=lq.page, per_page=lq.per_page,
    )

    # for inline editing: limit a workflow status cell to its valid next states
    wf_map = workflow.for_table(session, mf.table_id)
    wf_cols = {c.column: wf_map[c.meta.id] for c in lq.columns
               if c.kind == "field" and c.meta.data_type == "enum" and c.meta.id in wf_map}
    wf_cell_options = {}
    for row in rows:
        for col, wf in wf_cols.items():
            wf_cell_options.setdefault(row[mf.table.pk_col], {})[col] = \
                workflow.allowed_choices(wf, row.get(col), current_user)

    enum_colors = {c.column: json.loads(c.meta.enum_colors) for c in lq.columns
                   if c.kind == "field" and c.meta.data_type == "enum" and c.meta.enum_colors}
    show_sla = sla.has_policies(session, mf.table_id)
    sla_map = sla.clocks_for_rows(
        session, mf.table_id, mf.table.phys_name,
        [r[mf.table.pk_col] for r in rows]) if show_sla else {}
    maint = maintenance.is_active(session, mf.table)

    all_args = request.args.to_dict(flat=False)
    pages = max(1, (total + lq.per_page - 1) // lq.per_page)
    saved_views = session.scalars(
        select(SavedView).where(SavedView.user_id == user_id, SavedView.form_id == mf.id)
        .order_by(SavedView.name)).all()
    args_pg = {k: v for k, v in all_args.items() if k != "page"}
    return render_template(
        "user/list.html", mf=mf, columns=lq.columns, rows=rows, q=lq.q,
        page=lq.page, pages=pages, total=total, sort=lq.sort, order=lq.order,
        per_page=lq.per_page, allowed_per_page=ALLOWED_PER_PAGE,
        filter_meta=lq.filter_meta, filter_order=lq.filter_order, conditions=lq.conditions,
        filter_chips=_filter_chips(mf, lq, all_args), enum_colors=enum_colors,
        show_sla=show_sla, sla_map=sla_map, maint=maint,
        can_edit=can_write(access), has_trash=mf.table.soft_delete,
        label_maps=lq.label_maps, m1_targets=lq.m1_targets,
        display_col=display_field_name(session, mf.table), view_table_id=mf.table.id,
        pk_col=mf.table.pk_col,
        saved_views=saved_views, current_query=urlencode(args_pg, doseq=True),
        args_pg=args_pg, wf_cols=set(wf_cols), wf_cell_options=wf_cell_options,
        has_enum=any(f.data_type == "enum" for f in mf.table.fields),
        has_dates=any(f.data_type in ("date", "datetime") for f in mf.table.fields),
        has_feeds=bool(_manual_feeds(session, mf.table_id)),
        args_sort={k: v for k, v in all_args.items()
                   if k not in ("page", "sort", "order")},
    )


@bp.route("/forms/<int:form_id>/kanban")
def form_kanban(form_id):
    mf, access = _require(form_id, "read")
    session = _s()
    engine = engine_for_table(mf.table)
    readable = helpers.readable_fields(session, current_user, mf.table)
    enum_fields = [f for f in mf.table.fields
                   if f.data_type == "enum" and f.phys_name in readable]
    if not enum_fields:
        flash("This table has no choice (enum) field to group by.", "info")
        return redirect(url_for("user.form_list", form_id=form_id))
    wf_field_ids = set(workflow.for_table(session, mf.table_id))
    group = request.args.get("group")
    field = next((f for f in enum_fields if f.phys_name == group), None) \
        or next((f for f in enum_fields if f.id in wf_field_ids), enum_fields[0])
    options = json.loads(field.enum_options or "[]")

    user_id, is_designer = _ctx()
    rows, _total = record_service.list_records(
        engine, mf.table, user_id=user_id, is_designer=is_designer, per_page=None)
    disp = display_field_name(session, mf.table)
    extra_cols = [f.phys_name for f in mf.table.fields
                  if f.data_type in ("string", "integer", "bigint", "decimal", "float",
                                     "date", "datetime", "time", "boolean")
                  and f.phys_name not in (disp, field.phys_name)
                  and f.phys_name in readable][:2]

    def _card(r):
        title = r.get(disp)
        pk = r[mf.table.pk_col]
        return {"pk": pk, "title": str(title) if title not in (None, "") else f"#{pk}",
                "extras": [r.get(c) for c in extra_cols if r.get(c) not in (None, "")]}

    cap, buckets, unset = 200, {o: [] for o in options}, []
    for r in rows:
        v = r.get(field.phys_name)
        (buckets[v] if v in buckets else unset).append(r)
    columns = [{"value": o, "count": len(buckets[o]), "cards": [_card(r) for r in buckets[o][:cap]]}
               for o in options]
    if unset:
        columns.append({"value": "", "count": len(unset),
                        "cards": [_card(r) for r in unset[:cap]]})
    return render_template("user/kanban.html", mf=mf, field=field, enum_fields=enum_fields,
                           columns=columns, group=field.phys_name, can_edit=can_write(access),
                           view_table_id=mf.table.id,
                           has_dates=any(f.data_type in ("date", "datetime") for f in mf.table.fields))


@bp.route("/forms/<int:form_id>/calendar")
def form_calendar(form_id):
    mf, access = _require(form_id, "read")
    session = _s()
    engine = engine_for_table(mf.table)
    date_fields = [f for f in mf.table.fields if f.data_type in ("date", "datetime")]
    if not date_fields:
        flash("This table has no date field to show on a calendar.", "info")
        return redirect(url_for("user.form_list", form_id=form_id))
    dname = request.args.get("date")
    field = next((f for f in date_fields if f.phys_name == dname), None) or date_fields[0]

    today = date.today()
    try:
        y, m = (int(x) for x in (request.args.get("month") or "").split("-"))
        first = date(y, m, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    nxt = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
    prev = date(first.year - 1, 12, 1) if first.month == 1 else date(first.year, first.month - 1, 1)

    user_id, is_designer = _ctx()
    rows, _total = record_service.list_records(
        engine, mf.table, user_id=user_id, is_designer=is_designer, per_page=None,
        filters=[{"col": field.phys_name, "op": "gte", "value": first.isoformat()},
                 {"col": field.phys_name, "op": "lt", "value": nxt.isoformat()}])
    disp = display_field_name(session, mf.table)
    by_day = {}
    for r in rows:
        v = r.get(field.phys_name)
        if v is None:
            continue
        day = v.date() if hasattr(v, "hour") else v
        title = r.get(disp)
        by_day.setdefault(day.isoformat(), []).append(
            {"pk": r[mf.table.pk_col],
             "title": str(title) if title not in (None, "") else f"#{r[mf.table.pk_col]}"})

    weeks = _calendar.Calendar().monthdatescalendar(first.year, first.month)
    return render_template(
        "user/calendar.html", mf=mf, field=field, date_fields=date_fields, weeks=weeks,
        by_day=by_day, first=first, prev=prev, nxt=nxt, today=today, can_edit=can_write(access),
        view_table_id=mf.table.id, weekdays=_calendar.day_abbr,
        has_enum=any(f.data_type == "enum" for f in mf.table.fields))


def _csv_download(text, filename):
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.route("/forms/<int:form_id>/export.csv")
def export_list_csv(form_id):
    mf, _a = _require(form_id, "read")
    session = _s()
    engine = engine_for_table(mf.table)
    lq = _list_query(mf, session, engine)
    user_id, is_designer = _ctx()
    rows, _total = record_service.list_records(
        engine, mf.table, user_id=user_id, is_designer=is_designer,
        filters=lq.filters, sort=lq.sort, order=lq.order, per_page=None,
    )
    csv_text = list_export.list_csv(lq.columns, rows, lq.label_maps)
    return _csv_download(csv_text, f"{mf.table.phys_name}.csv")


# --------------------------------------------------------------------------- #
# Bulk actions
# --------------------------------------------------------------------------- #
def _selected_ids():
    return [i for i in request.form.getlist("ids") if i not in (None, "")]


@bp.route("/forms/<int:form_id>/bulk/delete", methods=["POST"])
def bulk_delete(form_id):
    mf, _a = _require(form_id, "write")
    ids = _selected_ids()
    if not ids:
        flash("No rows selected.", "info")
        return redirect(_safe_next(url_for("user.form_list", form_id=form_id)))
    return render_template("user/bulk_delete_confirm.html", mf=mf, ids=ids,
                           hard=not mf.table.soft_delete,
                           next=_safe_next(url_for("user.form_list", form_id=form_id)))


@bp.route("/forms/<int:form_id>/bulk/delete/confirm", methods=["POST"])
def bulk_delete_run(form_id):
    mf, _a = _require(form_id, "write")
    session = _s()
    engine = engine_for_table(mf.table)
    done, failed = 0, 0
    for pk in _selected_ids():
        try:
            record_service.remove(session, engine, mf.table, pk, current_user_id())
            done += 1
        except Exception:  # noqa: BLE001 - e.g. FK restrict; count and continue
            failed += 1
    flash(f"Deleted {done}." if not failed else f"Deleted {done}, failed {failed}.",
          "info" if not failed else "warning")
    return redirect(_safe_next(url_for("user.form_list", form_id=form_id)))


@bp.route("/forms/<int:form_id>/bulk/export.csv", methods=["POST"])
def bulk_export(form_id):
    mf, _a = _require(form_id, "read")
    session = _s()
    engine = engine_for_table(mf.table)
    user_id, is_designer = _ctx()
    lq = _list_query(mf, session, engine)

    def _visible(row):
        if mf.table.soft_delete and row.get("deleted_at") is not None:
            return False
        if mf.table.row_owned and not is_designer and row.get("created_by") not in (None, user_id):
            return False
        return True

    rows = [r for r in data_service.rows_by_ids(engine, mf.table.phys_name, _selected_ids())
            if _visible(r)]
    csv_text = list_export.list_csv(lq.columns, rows, lq.label_maps)
    return _csv_download(csv_text, f"{mf.table.phys_name}_selected.csv")


def _manual_feeds(session, table_id):
    return session.scalars(
        select(Feed).where(Feed.source_table_id == table_id, Feed.active.is_(True),
                           Feed.allow_manual.is_(True)).order_by(Feed.id)).all()


@bp.route("/forms/<int:form_id>/send", methods=["POST"])
def feed_send(form_id):
    """Push the selected rows to connected tools via every manual feed on this table."""
    mf, _a = _require(form_id, "read")
    session = _s()
    engine = engine_for_table(mf.table)
    ids = _selected_ids()
    dest = _safe_next(url_for("user.form_list", form_id=form_id))
    if not ids:
        flash("No rows selected.", "info")
        return redirect(dest)
    fds = _manual_feeds(session, mf.table_id)
    if not fds:
        flash("No manual feeds are configured for this table.", "warning")
        return redirect(dest)
    sent = failed = 0
    for feed in fds:
        for status in feeds.run_manual(session, engine, feed, mf.table, ids, current_user_id()):
            sent += status == "sent"
            failed += status == "failed"
    msg = f"Sent {sent} record(s) to connected tools."
    flash(msg if not failed else msg + f" {failed} failed.", "success" if not failed else "warning")
    return redirect(dest)


# --------------------------------------------------------------------------- #
# Saved views (per user)
# --------------------------------------------------------------------------- #
def _list_with_query(form_id, query):
    url = url_for("user.form_list", form_id=form_id)
    return redirect(f"{url}?{query}" if query else url)


@bp.route("/forms/<int:form_id>/views", methods=["POST"])
def view_save(form_id):
    _require(form_id, "read")
    session = _s()
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Give the view a name.", "warning")
        return _list_with_query(form_id, request.form.get("query", ""))
    session.add(SavedView(user_id=current_user_id(), form_id=form_id, name=name,
                          query=request.form.get("query", "")))
    session.commit()
    flash(f"Saved view “{name}”.", "success")
    return _list_with_query(form_id, request.form.get("query", ""))


@bp.route("/forms/<int:form_id>/views/<int:vid>")
def view_apply(form_id, vid):
    _require(form_id, "read")
    sv = _s().get(SavedView, vid)
    if not sv or sv.user_id != current_user_id() or sv.form_id != form_id:
        abort(404)
    return _list_with_query(form_id, sv.query or "")


@bp.route("/forms/<int:form_id>/views/<int:vid>/delete", methods=["POST"])
def view_delete(form_id, vid):
    _require(form_id, "read")
    session = _s()
    sv = session.get(SavedView, vid)
    if sv and sv.user_id == current_user_id() and sv.form_id == form_id:
        session.delete(sv)
        session.commit()
        flash("View deleted.", "info")
    return redirect(url_for("user.form_list", form_id=form_id))


# --------------------------------------------------------------------------- #
# Reports (group-by / aggregation) — scoped to what the user may read
# --------------------------------------------------------------------------- #
def _report_query(args):
    return urlencode([(k, v) for k, v in args.items(multi=True) if k != "export"])


@bp.route("/report/<int:table_id>")
def report(table_id):
    session = _s()
    table = session.get(MetaTable, table_id)
    if not table:
        abort(404)
    engine = engine_for_table(table)
    if not _table_readable(session, table):
        abort(403)
    user_id, is_designer = _ctx()
    scope = record_service._scope_filters(
        table, user_id=user_id, is_designer=is_designer, include_deleted=False)
    ctx = reporting.build(session, engine, table, request.args, base_filters=scope, user=current_user)
    if request.args.get("export") == "csv":
        return _csv_download(reporting.to_csv(ctx["result"]), f"{table.phys_name}_report.csv")
    saved = session.scalars(select(ReportDef).where(
        ReportDef.user_id == user_id, ReportDef.table_id == table.id)
        .order_by(ReportDef.name)).all()
    add_dashboards = session.scalars(
        select(Dashboard).where(Dashboard.owner_user_id == user_id).order_by(Dashboard.name)).all()
    if current_user.is_designer:                      # designers may also add to shared dashboards
        add_dashboards = list(add_dashboards) + list(session.scalars(
            select(Dashboard).where(Dashboard.owner_user_id.is_(None)).order_by(Dashboard.name)))
    return render_template("user/report.html",
                           action=url_for("user.report", table_id=table_id), tables=None,
                           saved_reports=saved, current_query=_report_query(request.args),
                           add_dashboards=add_dashboards, add_table_id=table.id, **ctx)


@bp.route("/reports/<int:table_id>", methods=["POST"])
def report_save(table_id):
    session = _s()
    name = (request.form.get("name") or "").strip()
    if name:
        session.add(ReportDef(
            user_id=current_user_id(), table_id=table_id, name=name,
            query=request.form.get("query", ""),
            schedule_minutes=request.form.get("schedule_minutes", type=int) or None,
            recipients=(request.form.get("recipients") or "").strip() or None))
        session.commit()
        flash(f"Saved report “{name}”.", "success")
    return redirect(_safe_next(url_for("user.report", table_id=table_id)))


@bp.route("/reports/<int:report_id>/schedule", methods=["POST"])
def report_schedule(report_id):
    """Set or clear a saved report's email-digest schedule (blank/0 minutes clears)."""
    session = _s()
    r = session.get(ReportDef, report_id)
    default = url_for("user.report", table_id=r.table_id) if r else url_for("user.dashboard")
    if r and r.user_id == current_user_id():
        r.schedule_minutes = request.form.get("schedule_minutes", type=int) or None
        r.recipients = (request.form.get("recipients") or "").strip() or None
        session.commit()
        flash("Report schedule updated." if r.schedule_minutes else "Report schedule cleared.", "info")
    return redirect(_safe_next(default))


@bp.route("/reports/<int:report_id>/delete", methods=["POST"])
def report_delete(report_id):
    session = _s()
    r = session.get(ReportDef, report_id)
    default = url_for("user.report", table_id=r.table_id) if r else url_for("user.dashboard")
    if r and r.user_id == current_user_id():
        session.delete(r)
        session.commit()
        flash("Report deleted.", "info")
    return redirect(_safe_next(default))


@bp.route("/reports/<int:report_id>/pin", methods=["POST"])
def report_pin(report_id):
    session = _s()
    r = session.get(ReportDef, report_id)
    if r and r.user_id == current_user_id():
        r.pinned = not r.pinned
        session.commit()
        flash("Pinned to dashboard." if r.pinned else "Unpinned.", "info")
    return redirect(_safe_next(url_for("user.dashboard")))


# --------------------------------------------------------------------------- #
# Create / edit / clone
# --------------------------------------------------------------------------- #
@bp.route("/forms/<int:form_id>/new", methods=["GET", "POST"])
def record_new(form_id):
    _require(form_id, "write")
    return _save_record(form_id, pk=None, clone_from=None)


@bp.route("/forms/<int:form_id>/<pk>/edit", methods=["GET", "POST"])
def record_edit(form_id, pk):
    _require(form_id, "write")
    return _save_record(form_id, pk=pk, clone_from=None)


@bp.route("/forms/<int:form_id>/<pk>/clone", methods=["GET", "POST"])
def record_clone(form_id, pk):
    _require(form_id, "write")
    return _save_record(form_id, pk=None, clone_from=pk)


# --------------------------------------------------------------------------- #
# Inline cell editing (scalar / enum / boolean fields)
# --------------------------------------------------------------------------- #
_INLINE_TYPES = {"string", "text", "integer", "bigint", "decimal", "float",
                 "boolean", "date", "datetime", "time", "enum",
                 "email", "url", "phone", "currency", "percent"}
_BOOL_TRUE = {"1", "true", "yes", "on"}


def _cell_display(field, value):
    if field.data_type == "boolean":
        return "yes" if value else "no"
    return "—" if value in (None, "") else str(value)


def _cell_value(field, value):
    if field.data_type == "boolean":
        return "1" if value else "0"
    return "" if value is None else str(value)


@bp.route("/forms/<int:form_id>/<pk>/cell", methods=["POST"])
def cell_update(form_id, pk):
    mf, _a = _require(form_id, "write")
    session = _s()
    engine = engine_for_table(mf.table)
    built = build_form(mf, session, engine, current_user)
    display_col = display_field_name(session, mf.table)
    col = request.form.get("col")
    item = next((it for it in built.items
                 if it.kind == "field" and it.column == col and not it.readonly
                 and it.meta.data_type in _INLINE_TYPES and it.column != display_col), None)
    if not item:
        return jsonify(ok=False, error="This field can't be edited here."), 400

    field = item.meta
    raw = (request.form.get("value") or "").strip()
    try:
        if field.data_type == "boolean":
            value = raw.lower() in _BOOL_TRUE
        else:
            value = importer.coerce_value(field, raw)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    if value is None and not field.nullable:
        return jsonify(ok=False, error=f"{field.label} is required."), 400

    user_id, is_designer = _ctx()
    old = record_service.get_record(engine, mf.table, pk,
                                    user_id=user_id, is_designer=is_designer)
    if not old:
        abort(404)
    values = {col: value}
    diverted = approvals.plan_diversions(session, mf.table, old, values)
    try:  # honour status-workflow transitions here too (not just on the edit form)
        workflow.check(session, mf.table, old, values, current_user)
    except workflow.WorkflowError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    for d in diverted:
        approvals.request_transition(session, engine, mf.table, d["wf"], pk,
                                     d["frm"], d["to"], current_user)
    if values:
        record_service.update(session, engine, mf.table, pk, values, user_id)
    if diverted and col not in values:  # this cell's change is held for approval
        held = old.get(col)
        return jsonify(ok=True, pending=True, display=_cell_display(field, held),
                       value=_cell_value(field, held))
    return jsonify(ok=True, display=_cell_display(field, value),
                   value=_cell_value(field, value))


def _save_record(form_id, pk, clone_from):
    session = _s()
    mf = _get_form(session, form_id)
    engine = engine_for_table(mf.table)
    built = build_form(mf, session, engine, current_user)
    user_id, is_designer = _ctx()

    if request.method == "POST":
        form = built.form_class()
        if form.validate():
            values, mn = _collect(built, form)
            ok, diverted = True, []
            if pk is not None:
                old = record_service.get_record(engine, mf.table, pk,
                                                user_id=user_id, is_designer=is_designer) or {}
                diverted = approvals.plan_diversions(session, mf.table, old, values)
                try:
                    workflow.check(session, mf.table, old, values, current_user)
                except workflow.WorkflowError as exc:
                    flash(str(exc), "danger")
                    ok = False
            if ok:
                if pk is None:
                    new_pk = record_service.create(session, engine, mf.table, values, user_id)
                else:
                    if values:
                        record_service.update(session, engine, mf.table, pk, values, user_id)
                    for d in diverted:
                        approvals.request_transition(session, engine, mf.table, d["wf"], pk,
                                                     d["frm"], d["to"], current_user)
                    new_pk = pk
                for item, ids in mn:
                    data_service.set_links(
                        engine, item.junction, item.this_col, new_pk, item.other_col, ids
                    )
                for item in built.items:
                    if item.kind == "file":
                        _save_attachments(session, item, new_pk)
                if diverted:
                    flash("Submitted for approval: " + ", ".join(
                        f"{d['frm']} → {d['to']}" for d in diverted), "success")
                else:
                    flash("Saved.", "success")
                return redirect(_safe_next(url_for("user.form_list", form_id=form_id)))
    else:
        defaults = {}
        source_pk = clone_from if clone_from is not None else pk
        if source_pk is not None:
            row = record_service.get_record(engine, mf.table, source_pk,
                                            user_id=user_id, is_designer=is_designer)
            if row:
                for it in built.items:
                    if it.kind == "field" and it.meta.data_type == "tags":
                        try:
                            defaults[it.name] = json.loads(row.get(it.column) or "[]")
                        except (ValueError, TypeError):
                            defaults[it.name] = []
                    elif it.kind in ("field", "relation_m1"):
                        defaults[it.name] = row.get(it.column)
                    elif it.kind == "relation_mn" and clone_from is None:
                        defaults[it.name] = data_service.get_links(
                            engine, it.junction, it.this_col, source_pk, it.other_col
                        )
        else:
            # new record: designer defaults, then query-arg prefill (e.g. ?customer_id=5)
            _tokens = ("now", "today", "current_user", "me")
            for it in built.items:
                if (it.kind == "field" and it.meta.default_value not in (None, "")
                        and it.meta.default_value.strip().lower() not in _tokens):
                    try:
                        defaults[it.name] = importer.coerce_value(it.meta, it.meta.default_value)
                    except ValueError:
                        pass
            for it in built.items:
                if it.kind in ("field", "relation_m1") and request.args.get(it.name):
                    raw = request.args.get(it.name)
                    try:  # coerce so e.g. a date prefill becomes a date object
                        defaults[it.name] = importer.coerce_value(it.meta, raw)
                    except ValueError:
                        defaults[it.name] = raw
        form = built.form_class(data=defaults)

    _apply_workflow_choices(session, engine, mf, built, form, pk, user_id, is_designer)
    mode = "Clone" if clone_from is not None else ("Edit" if pk else "New")
    related = _related_lists(session, engine, mf, pk, user_id, is_designer) if pk else []
    history = _history(session, mf, pk) if (pk and mf.table.track_audit) else []
    return render_template("user/record_form.html", mf=mf, form=form,
                           items=built.items, mode=mode, pk=pk,
                           next=_safe_next(None), related=related, history=history,
                           attachments=_attachments_map(session, built, pk),
                           sections=_split_sections(built.items))


def _apply_workflow_choices(session, engine, mf, built, form, pk, user_id, is_designer):
    """Limit a workflow status field to valid next states (edit), default it (new)."""
    wfs = workflow.for_table(session, mf.table_id)
    if not wfs:
        return
    current = {}
    if pk is not None:
        current = record_service.get_record(engine, mf.table, pk, user_id=user_id,
                                            is_designer=is_designer) or {}
    for it in built.items:
        if it.kind != "field" or it.meta.data_type != "enum" or it.meta.id not in wfs:
            continue
        wf = wfs[it.meta.id]
        field = getattr(form, it.name, None)
        if field is None:
            continue
        if pk is not None:
            cur = current.get(it.column)
            choices = workflow.allowed_choices(wf, cur, current_user)
            for c in approvals.extra_choices(session, wf, cur):  # approval-required targets
                if c not in choices:
                    choices.append(c)
            field.choices = [(c, c) for c in choices]
        elif wf.initial_state and not field.data:
            field.data = wf.initial_state


def _split_sections(items):
    """Group form items by 'section' breaks → [{label, fields}]; [] if none.

    ('fields' rather than 'items' to avoid Jinja resolving ``g.items`` to dict.items.)
    """
    if not any(it.kind == "section" for it in items):
        return []
    groups, current = [], {"label": "General", "fields": []}
    for it in items:
        if it.kind == "section":
            if current["fields"]:
                groups.append(current)
            current = {"label": it.label, "fields": []}
        else:
            current["fields"].append(it)
    if current["fields"]:
        groups.append(current)
    return groups


# --------------------------------------------------------------------------- #
# Attachments (file / image fields)
# --------------------------------------------------------------------------- #
def _attachments_map(session, built, pk):
    """{field_id: [Attachment, ...]} for the file items of this record."""
    out = {}
    if not pk:
        return out
    for item in built.items:
        if item.kind == "file":
            out[item.meta.id] = session.scalars(
                select(Attachment).where(Attachment.field_id == item.meta.id,
                                         Attachment.row_pk == pk)
                .order_by(Attachment.id)).all()
    return out


def _save_attachments(session, item, pk):
    """Apply removals (rm_<field_id>) then additions (file_<field_id>) for a record."""
    field = item.meta
    rm_ids = {int(i) for i in request.form.getlist(f"rm_{field.id}") if i.isdigit()}
    if rm_ids:
        for att in session.scalars(select(Attachment).where(
                Attachment.field_id == field.id, Attachment.row_pk == pk,
                Attachment.id.in_(rm_ids))):
            file_store.delete(field.id, att.stored_name)
            session.delete(att)
    for fs in request.files.getlist(f"file_{field.id}"):
        if not fs or not fs.filename:
            continue
        try:
            meta = file_store.save(fs, field)
        except file_store.UploadError as exc:
            flash(str(exc), "warning")
            continue
        session.add(Attachment(field_id=field.id, row_pk=pk,
                               uploaded_by=current_user_id(), **meta))
    session.commit()


def _table_readable(session, table):
    return helpers.table_readable(session, current_user, table)


@bp.route("/attachment/<int:att_id>")
def attachment(att_id):
    session = _s()
    att = session.get(Attachment, att_id)
    field = session.get(MetaField, att.field_id) if att else None
    table = session.get(MetaTable, field.table_id) if field else None
    if not att or not field or not table:
        abort(404)
    if not _table_readable(session, table):
        abort(403)
    engine = engine_for_table(table)
    user_id, is_designer = _ctx()
    if not record_service.get_record(engine, table, att.row_pk, user_id=user_id,
                                     is_designer=is_designer, allow_deleted=True):
        abort(404)
    path = file_store.abs_path(att.field_id, att.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=att.content_type or "application/octet-stream",
                     download_name=att.original_name, as_attachment=False)


def _record_label(session, engine, mf, pk):
    row = data_service.get_row(engine, mf.table.phys_name, pk)
    if not row:
        return f"#{pk}"
    val = row.get(display_field_name(session, mf.table))
    return str(val) if val not in (None, "") else f"#{pk}"


@bp.route("/forms/<int:form_id>/<pk>/confirm-delete")
def record_delete_confirm(form_id, pk):
    mf, _a = _require(form_id, "write")
    session = _s()
    engine = engine_for_table(mf.table)
    hard = not mf.table.soft_delete
    impact = record_service.delete_impact(session, engine, mf.table, pk, hard=hard)
    return render_template(
        "user/delete_confirm.html", mf=mf, pk=pk, impact=impact, hard=hard,
        label=_record_label(session, engine, mf, pk), next=_safe_next(None),
        action=url_for("user.record_delete", form_id=form_id, pk=pk),
    )


@bp.route("/forms/<int:form_id>/<pk>/confirm-destroy")
def record_destroy_confirm(form_id, pk):
    mf, _a = _require(form_id, "write")
    session = _s()
    engine = engine_for_table(mf.table)
    impact = record_service.delete_impact(session, engine, mf.table, pk, hard=True)
    return render_template(
        "user/delete_confirm.html", mf=mf, pk=pk, impact=impact, hard=True,
        label=_record_label(session, engine, mf, pk),
        next=_safe_next(url_for("user.form_trash", form_id=form_id)),
        action=url_for("user.record_destroy", form_id=form_id, pk=pk),
    )


@bp.route("/forms/<int:form_id>/<pk>/delete", methods=["POST"])
def record_delete(form_id, pk):
    mf, _a = _require(form_id, "write")
    session = _s()
    engine = engine_for_table(mf.table)
    try:
        record_service.remove(session, engine, mf.table, pk, current_user_id())
        flash("Moved to Trash." if mf.table.soft_delete else "Deleted.", "info")
    except Exception as exc:  # noqa: BLE001 - e.g. FK restrict
        flash(f"Could not delete: {exc}", "danger")
    return redirect(_safe_next(url_for("user.form_list", form_id=form_id)))


# --------------------------------------------------------------------------- #
# Trash (soft delete)
# --------------------------------------------------------------------------- #
@bp.route("/forms/<int:form_id>/trash")
def form_trash(form_id):
    mf, _a = _require(form_id, "write")
    if not mf.table.soft_delete:
        flash("Soft delete is not enabled for this table.", "info")
        return redirect(url_for("user.form_list", form_id=form_id))
    session = _s()
    engine = engine_for_table(mf.table)
    built = build_form(mf, session, engine, current_user)
    columns = [it for it in built.items if it.kind in ("field", "relation_m1")]
    user_id, is_designer = _ctx()
    rows, total = record_service.list_records(
        engine, mf.table, user_id=user_id, is_designer=is_designer,
        include_deleted=True, per_page=200,
    )
    return render_template("user/trash.html", mf=mf, columns=columns, rows=rows, total=total)


@bp.route("/forms/<int:form_id>/<pk>/restore", methods=["POST"])
def record_restore(form_id, pk):
    mf, _a = _require(form_id, "write")
    record_service.restore(_s(), engine_for_table(mf.table), mf.table, pk, current_user_id())
    flash("Restored.", "success")
    return redirect(url_for("user.form_trash", form_id=form_id))


@bp.route("/forms/<int:form_id>/<pk>/destroy", methods=["POST"])
def record_destroy(form_id, pk):
    mf, _a = _require(form_id, "write")
    record_service.destroy(_s(), engine_for_table(mf.table), mf.table, pk, current_user_id())
    flash("Permanently deleted.", "info")
    return redirect(url_for("user.form_trash", form_id=form_id))


# --------------------------------------------------------------------------- #
# Read-only record view
# --------------------------------------------------------------------------- #
@bp.route("/view/<int:table_id>/<pk>")
def record_view(table_id, pk):
    session = _s()
    table = session.get(MetaTable, table_id)
    view_form = table_view_form(session, table_id)
    if not table or not view_form:
        abort(404)
    engine = engine_for_table(table)
    if not can_read(form_access(session, current_user, view_form.id)):
        abort(403)
    user_id, is_designer = _ctx()
    row = record_service.get_record(engine, table, pk, user_id=user_id,
                                    is_designer=is_designer, allow_deleted=True)
    if not row:
        abort(404)

    built = build_form(view_form, session, engine, current_user)
    items = _view_items(session, engine, view_form, built, row)
    disp = display_field_name(session, table)
    label = str(row.get(disp)) if row.get(disp) not in (None, "") else f"#{pk}"

    edit_url = None
    for f in session.scalars(select(MetaForm).where(MetaForm.table_id == table_id,
                                                    MetaForm.purpose != "view").order_by(MetaForm.id)):
        if can_write(form_access(session, current_user, f.id)):
            edit_url = url_for("user.record_edit", form_id=f.id, pk=pk)
            break

    send_form_id = view_form.id if _manual_feeds(session, table_id) else None
    related = _related_lists(session, engine, view_form, pk, user_id, is_designer,
                             parent_url=url_for("user.record_view", table_id=table_id, pk=pk))
    history = _history(session, view_form, pk) if table.track_audit else []
    sla_clocks = sla.clocks_for_record(session, table_id, table.phys_name, pk)
    approval_reqs = approvals.requests_for_record(session, table.phys_name, pk)
    can_approve = {r["req"].id: approvals.can_act(session, r["req"], current_user)
                   for r in approval_reqs}
    lf = _first_readable_form(session, table_id)
    list_url = url_for("user.form_list", form_id=lf.id) if lf else None
    assign_field = next((f for f in table.fields if f.data_type == "user"), None)
    can_assign = bool(assign_field is not None and edit_url
                      and row.get(assign_field.phys_name) != user_id)
    maint_windows = [{"w": w, "status": maintenance.status(w)}
                     for w in maintenance.for_record(session, table_id, pk)]
    return render_template("user/view.html", table=table, pk=pk, label=label, items=items,
                           can_assign=can_assign,
                           maint=maintenance.is_active(session, table),
                           maint_windows=maint_windows,
                           watching=watch.is_watching(session, user_id,
                                                      table.phys_name, pk),
                           list_url=list_url,
                           edit_url=edit_url, deleted=bool(row.get("deleted_at")),
                           send_form_id=send_form_id, related=related, history=history,
                           sla_clocks=sla_clocks, approval_reqs=approval_reqs,
                           can_approve=can_approve,
                           thread=comments.list_for(session, table.phys_name, pk,
                                                    include_internal=True))


# --------------------------------------------------------------------------- #
# Bulk edit
# --------------------------------------------------------------------------- #
_BULK_SKIP_TYPES = {"json", "tags", "autonumber", "formula", "file", "image"}


def _bulk_editable_fields(session, mf):
    """Fields a bulk edit may set: writable scalars, pickers and m:1 columns."""
    from ..helpers import _field_perm_map
    fperm = {} if current_user.is_designer else _field_perm_map(session, current_user)
    return [f for f in mf.table.fields
            if f.data_type not in _BULK_SKIP_TYPES
            and f.phys_name != mf.table.pk_col
            and fperm.get(f.id, "write") == "write"]


@bp.route("/forms/<int:form_id>/bulk-edit", methods=["POST"])
def bulk_edit_form(form_id):
    mf, _access = _require(form_id, "write")
    ids = request.form.getlist("ids")
    if not ids:
        flash("Select at least one row first.", "warning")
        return redirect(_safe_next(url_for("user.form_list", form_id=form_id)))
    return render_template("user/bulk_edit.html", mf=mf, ids=ids,
                           fields=_bulk_editable_fields(_s(), mf),
                           next=_safe_next(None))


@bp.route("/forms/<int:form_id>/bulk-edit/apply", methods=["POST"])
def bulk_edit_apply(form_id):
    mf, _access = _require(form_id, "write")
    session = _s()
    engine = engine_for_table(mf.table)
    back = redirect(_safe_next(url_for("user.form_list", form_id=form_id)))

    ids = request.form.getlist("ids")
    col = request.form.get("column")
    field = next((f for f in _bulk_editable_fields(session, mf)
                  if f.phys_name == col), None)
    if field is None or not ids:
        flash("Pick a field to change.", "warning")
        return back

    if request.form.get("clear"):
        if not field.nullable:
            flash("That field is required — it can't be cleared.", "danger")
            return back
        value = None
    else:
        resolver = None
        if field.data_type == RELATION_TYPE:
            resolver = importer._RelationResolver(session, engine, field)
        elif field.data_type == "user":
            resolver = importer._UserResolver(session)
        elif field.data_type == "company":
            resolver = importer._CompanyResolver(session)
        try:
            value = importer.coerce_value(field, request.form.get("value"), resolver)
        except ValueError as exc:
            flash(str(exc), "danger")
            return back
        if value is None and not field.nullable:
            flash("Enter a value.", "warning")
            return back

    user_id, is_designer = _ctx()
    done = skipped = 0
    for pk in ids:
        row = record_service.get_record(engine, mf.table, pk,
                                        user_id=user_id, is_designer=is_designer)
        if not row or row.get(field.phys_name) == value:
            skipped += 1
            continue
        values = {field.phys_name: value}
        # approval-gated transitions are popped out — a bulk edit must not
        # silently file approval requests, so those rows are skipped instead
        if approvals.plan_diversions(session, mf.table, row, values) or not values:
            skipped += 1
            continue
        try:
            workflow.check(session, mf.table, row, values, current_user)
        except workflow.WorkflowError:
            skipped += 1
            continue
        record_service.update(session, engine, mf.table, pk, values, user_id)
        done += 1
    flash(f"Updated {done} record(s)" + (f" — {skipped} skipped." if skipped else "."),
          "success" if done else "warning")
    return back


_ACT_HUES = {"create": "green", "update": "blue", "delete": "red",
             "restore": "teal", "comment": "violet"}


@bp.route("/activity")
def activity():
    """Cross-table activity feed (changes + comments) — built for shift handover."""
    from ..metadata.models import Comment
    session = _s()
    try:
        hours = max(1, min(int(request.args.get("hours", 24)), 24 * 14))
    except (TypeError, ValueError):
        hours = 24
    table_f = request.args.get("table", "")
    user_f = request.args.get("user", "")
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    readable = {t.phys_name: t for t in session.scalars(select(MetaTable))
                if helpers.table_readable(session, current_user, t)}
    phys_list = [table_f] if table_f in readable else list(readable)
    aq = select(AuditLog).where(AuditLog.at >= since, AuditLog.table_phys.in_(phys_list))
    cq = select(Comment).where(Comment.created_at >= since,
                               Comment.table_phys.in_(phys_list))
    if user_f.isdigit():
        aq = aq.where(AuditLog.user_id == int(user_f))
        cq = cq.where(Comment.user_id == int(user_f))
    users_map = {u.id: u.username for u in session.scalars(select(AppUser))}

    items = []
    for a in session.scalars(aq.order_by(AuditLog.id.desc()).limit(100)):
        if a.row_pk is None:
            continue
        detail = ""
        if a.action == "update" and a.changes:
            try:
                detail = "changed: " + ", ".join(sorted(json.loads(a.changes)))
            except (ValueError, TypeError):
                pass
        items.append({"at": a.at, "kind": a.action,
                      "hue": _ACT_HUES.get(a.action, "gray"),
                      "username": users_map.get(a.user_id, "—"),
                      "table": readable[a.table_phys], "pk": a.row_pk,
                      "detail": detail})
    for c in session.scalars(cq.order_by(Comment.id.desc()).limit(100)):
        snippet = c.body if len(c.body) <= 80 else c.body[:79] + "…"
        items.append({"at": c.created_at, "kind": "comment", "hue": "violet",
                      "username": users_map.get(c.user_id, "—"),
                      "table": readable[c.table_phys], "pk": c.row_pk,
                      "detail": ("[internal] " if c.internal else "") + snippet})
    items.sort(key=lambda x: (x["at"] is None, x["at"]), reverse=True)
    items = items[:100]

    label_cache = {}                       # resolve labels only for shown items
    for it in items:
        key = (it["table"].phys_name, str(it["pk"]))
        if key not in label_cache:
            try:
                row = data_service.get_row(engine_for_table(it["table"]),
                                           it["table"].phys_name, it["pk"])
            except Exception:  # noqa: BLE001 - deleted rows / broken sources
                row = None
            v = row.get(display_field_name(session, it["table"])) if row else None
            label_cache[key] = str(v) if v not in (None, "") else f"#{it['pk']}"
        it["label"] = label_cache[key]

    return render_template(
        "user/activity.html", items=items, hours=hours, table_f=table_f,
        user_f=user_f, tables=sorted(readable.values(), key=lambda t: t.label),
        users=session.scalars(select(AppUser).order_by(AppUser.username)).all())


@bp.route("/watch/<int:table_id>/<pk>", methods=["POST"])
def watch_toggle(table_id, pk):
    """Subscribe to / unsubscribe from a record's updates and comments."""
    session = _s()
    table = session.get(MetaTable, table_id)
    view_form = table_view_form(session, table_id)
    if not table or not view_form:
        abort(404)
    if not can_read(form_access(session, current_user, view_form.id)):
        abort(403)
    user_id, is_designer = _ctx()
    if not record_service.get_record(engine_for_table(table), table, pk,
                                     user_id=user_id, is_designer=is_designer,
                                     allow_deleted=True):
        abort(404)
    now_watching = watch.toggle(session, user_id, table.phys_name, pk)
    flash("Watching this record — you'll be notified of updates and comments."
          if now_watching else "Stopped watching.", "info")
    return redirect(url_for("user.record_view", table_id=table_id, pk=pk))


@bp.route("/assign/<int:table_id>/<pk>", methods=["POST"])
def assign_to_me(table_id, pk):
    """One-click: set the table's (first) user field to the caller."""
    session = _s()
    table = session.get(MetaTable, table_id)
    if not table:
        abort(404)
    uf = next((f for f in table.fields if f.data_type == "user"), None)
    if uf is None:
        abort(404)
    writable = any(can_write(form_access(session, current_user, f.id))
                   for f in session.scalars(select(MetaForm).where(
                       MetaForm.table_id == table_id, MetaForm.purpose != "view")))
    if not writable:
        abort(403)
    user_id, is_designer = _ctx()
    engine = engine_for_table(table)
    if not record_service.get_record(engine, table, pk, user_id=user_id,
                                     is_designer=is_designer):
        abort(404)
    record_service.update(session, engine, table, pk, {uf.phys_name: user_id}, user_id)
    flash("Assigned to you.", "success")
    return redirect(_safe_next(url_for("user.record_view", table_id=table_id, pk=pk)))


@bp.route("/comments/<int:table_id>/<pk>", methods=["POST"])
def comment_add(table_id, pk):
    """Post a conversation entry (public reply or internal work note)."""
    session = _s()
    table = session.get(MetaTable, table_id)
    view_form = table_view_form(session, table_id)
    if not table or not view_form:
        abort(404)
    if not can_read(form_access(session, current_user, view_form.id)):
        abort(403)
    user_id, is_designer = _ctx()
    row = record_service.get_record(engine_for_table(table), table, pk, user_id=user_id,
                                    is_designer=is_designer, allow_deleted=True)
    if not row:
        abort(404)
    disp = display_field_name(session, table)
    label = str(row.get(disp)) if row.get(disp) not in (None, "") else f"#{pk}"
    try:
        comments.add(session, table.phys_name, pk, current_user,
                     request.form.get("body"),
                     internal=request.form.get("visibility") == "internal",
                     row=row, record_label=f"{table.label}: {label}")
        flash("Comment added.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("user.record_view", table_id=table_id, pk=pk))


@bp.route("/topology/<int:table_id>/<pk>")
def record_topology(table_id, pk):
    """Dependency & impact map for a single record (CI)."""
    session = _s()
    table = session.get(MetaTable, table_id)
    view_form = table_view_form(session, table_id)
    if not table or not view_form:
        abort(404)
    if not can_read(form_access(session, current_user, view_form.id)):
        abort(403)
    engine = engine_for_table(table)
    root = record_service.get_record(engine, table, pk, user_id=current_user_id(),
                                     is_designer=current_user.is_designer, allow_deleted=True)
    if not root:
        abort(404)
    pk = root.get(table.pk_col, pk)   # canonical, correctly-typed pk from the row

    max_depth = current_app.config["TOPOLOGY_MAX_DEPTH"]
    direction = request.args.get("direction", "both")
    if direction not in topology.DIRECTIONS:
        direction = "both"
    try:
        depth = int(request.args.get("depth", current_app.config["TOPOLOGY_DEFAULT_DEPTH"]))
    except (TypeError, ValueError):
        depth = current_app.config["TOPOLOGY_DEFAULT_DEPTH"]
    depth = max(1, min(depth, max_depth))

    graph = topology.graph_for(session, current_user, table, pk, direction=direction,
                               depth=depth, max_nodes=current_app.config["TOPOLOGY_MAX_NODES"])
    summary = {}
    for n in graph["nodes"]:
        summary[n["table_label"]] = summary.get(n["table_label"], 0) + 1
    summary = sorted(summary.items(), key=lambda kv: (-kv[1], kv[0]))
    disp = display_field_name(session, table)
    label = str(root.get(disp)) if root.get(disp) not in (None, "") else f"#{pk}"
    return render_template("user/topology.html", table=table, pk=pk, label=label,
                           graph=graph, summary=summary, direction=direction, depth=depth,
                           max_depth=max_depth)


def _view_items(session, engine, view_form, built, row):
    out = []
    row_pk = row.get(view_form.table.pk_col)
    for it in built.items:
        if it.kind == "file":
            atts = session.scalars(
                select(Attachment).where(Attachment.field_id == it.meta.id,
                                         Attachment.row_pk == row_pk)
                .order_by(Attachment.id)).all()
            out.append({"label": it.label, "kind": "file",
                        "is_image": it.meta.data_type == "image",
                        "files": [{"id": a.id, "name": a.original_name} for a in atts]})
        elif it.kind == "field":
            out.append({"label": it.label, "kind": "scalar", "value": row.get(it.column),
                        "data_type": it.meta.data_type,
                        "colors": json.loads(it.meta.enum_colors)
                        if it.meta.data_type == "enum" and it.meta.enum_colors else None})
        elif it.kind == "relation_m1":
            target, disp = m1_target_and_columns(session, it.meta)
            rid = row.get(it.column)
            lbls = data_service.labels_for(engine, target.phys_name, [rid], disp) if rid else []
            out.append({"label": it.label, "kind": "m1", "target_id": target.id, "ref_id": rid,
                        "ref_label": (lbls[0] if lbls else (str(rid) if rid else None))})
        elif it.kind == "relation_mn":
            rel = it.meta
            other_id = rel.to_table_id if rel.from_table_id == view_form.table_id else rel.from_table_id
            other = session.get(MetaTable, other_id)
            ids = data_service.get_links(engine, it.junction, it.this_col, row_pk, it.other_col)
            lbls = data_service.labels_for(engine, other.phys_name, ids,
                                           [display_field_name(session, other)])
            out.append({"label": it.label, "kind": "m2n", "other_table_id": other_id,
                        "refs": list(zip(ids, lbls))})
    return out


# --------------------------------------------------------------------------- #
# Related records (master-detail) + history helpers
# --------------------------------------------------------------------------- #
def _related_lists(session, engine, mf, pk, user_id, is_designer, parent_url=None):
    out = []
    # Where child Edit/Add/Delete actions return to. Defaults to this record's
    # edit form; the read-only view page passes its own URL so users come back here.
    parent_url = parent_url or url_for("user.record_edit", form_id=mf.id, pk=pk)
    rels = session.scalars(
        select(MetaRelation).where(MetaRelation.kind == "m1",
                                   MetaRelation.to_table_id == mf.table_id)
    ).all()
    for rel in rels:
        child = session.get(MetaTable, rel.from_table_id)
        fk = session.get(MetaField, rel.from_field_id) if rel.from_field_id else None
        if not child or not fk:
            continue
        child_form = session.scalar(
            select(MetaForm).where(MetaForm.table_id == child.id).order_by(MetaForm.id).limit(1))
        if not child_form:
            continue
        access = form_access(session, current_user, child_form.id)
        if not can_read(access):
            continue
        cols = [f.phys_name for f in child.fields if f.data_type != RELATION_TYPE][:4]
        rows, total = record_service.list_records(
            engine, child, user_id=user_id, is_designer=is_designer,
            filters=[{"col": fk.phys_name, "op": "eq", "value": pk}], per_page=50)
        out.append({
            "label": child.label, "columns": cols, "rows": rows, "total": total,
            "form_id": child_form.id, "table_id": child.id, "can_edit": can_write(access),
            "add_url": url_for("user.record_new", form_id=child_form.id,
                               next=parent_url, **{fk.phys_name: pk}),
            "next": parent_url,
        })
    return out


def _history(session, mf, pk):
    logs = session.scalars(
        select(AuditLog).where(AuditLog.table_phys == mf.table.phys_name,
                               AuditLog.row_pk == pk)
        .order_by(AuditLog.id.desc()).limit(50)
    ).all()
    names = {u.id: u.username for u in session.scalars(select(AppUser))}
    return [{
        "action": lg.action, "user": names.get(lg.user_id, "—"), "at": lg.at,
        "changes": json.loads(lg.changes) if lg.changes else None,
    } for lg in logs]


# --------------------------------------------------------------------------- #
# CSV import
# --------------------------------------------------------------------------- #
def _table_writable(session, table):
    return helpers.table_writable(session, current_user, table)


def _key_choices(table):
    """Candidate upsert keys: the primary key plus any unique field."""
    return [(table.pk_col, table.pk_col)] + [
        (f.phys_name, f.phys_name) for f in table.fields if f.is_unique]


@bp.route("/import")
def import_home():
    session = _s()
    table_id = request.args.get("table_id", type=int)
    table = session.get(MetaTable, table_id) if table_id else None
    if table and not _table_writable(session, table):
        abort(403)
    tables = [t for t in _all_tables(session) if _table_writable(session, t)]
    form = ImportForm()
    if table:
        form.key_column.choices = _key_choices(table)
    return render_template(
        "user/import.html", tables=tables, table=table,
        form=form, result=None,
        columns=_import_columns(session, table) if table else None,
    )


@bp.route("/import/<int:table_id>/template.csv")
def import_template(table_id):
    session = _s()
    table = session.get(MetaTable, table_id)
    if not table:
        abort(404)
    if not _table_writable(session, table):
        abort(403)
    return Response(
        importer.template_csv(table), mimetype="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename={table.phys_name}_template.csv"},
    )


@bp.route("/import/<int:table_id>", methods=["POST"])
def import_run(table_id):
    session = _s()
    table = session.get(MetaTable, table_id)
    if not table:
        abort(404)
    if not _table_writable(session, table):
        abort(403)
    form = ImportForm()
    form.key_column.choices = _key_choices(table)
    result = None
    if form.validate_on_submit():
        upsert = form.mode.data == "upsert"
        key_field = form.key_column.data if upsert else None
        if upsert and not key_field:
            flash("Choose a key column to match existing rows for upsert.", "warning")
        else:
            try:
                text = form.file.data.read().decode("utf-8-sig")
            except (UnicodeDecodeError, AttributeError):
                flash("Could not read the file as UTF-8 CSV.", "danger")
            else:
                allowed = (None if current_user.is_designer
                           else helpers.writable_fields(session, current_user, table))
                engine = engine_for_table(table)
                result = importer.import_rows(
                    session, engine, table, text,
                    skip_invalid=form.skip_invalid.data,
                    mode=form.mode.data, key_field=key_field, allowed=allowed)
                from .. import formula
                formula.recompute_table(session, engine, table)  # fill computed columns
                done = result["imported"] + result["updated"]
                if done:
                    flash(f"Imported {result['imported']}, updated {result['updated']} "
                          f"row(s) in {table.label}.", "success")
                elif not result["errors"]:
                    flash("No data rows found in the file.", "warning")
    return render_template(
        "user/import.html", tables=[t for t in _all_tables(session) if _table_writable(session, t)],
        table=table, form=form, result=result, columns=_import_columns(session, table),
    )


def _all_tables(session):
    return session.scalars(select(MetaTable).order_by(MetaTable.label)).all()


def _import_columns(session, table):
    """Column guide rows for the import page."""
    cols = []
    for f in importer.importable_fields(table):
        note = ""
        if f.data_type == RELATION_TYPE:
            target = session.get(MetaTable, f.related_table_id)
            if target:
                note = f"{target.label} id or {display_field_name(session, target)}"
        elif f.data_type == "enum":
            note = "one of: " + ", ".join(json.loads(f.enum_options or "[]"))
        cols.append({
            "name": f.phys_name, "type": type_label(f.data_type),
            "required": (not f.nullable) and (f.default_value in (None, "")),
            "note": note,
        })
    return cols


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _collect(built, form):
    values, mn = {}, []
    for item in built.items:
        if item.kind not in ("field", "relation_m1", "relation_mn"):
            continue  # e.g. 'file' items are handled via attachments, not WTForms
        if item.readonly:
            continue  # read-only (incl. field-permission 'read') — never written
        field = getattr(form, item.name)
        if item.kind == "field" and item.meta.data_type == "tags":
            values[item.column] = json.dumps(field.data or [])
        elif item.kind in ("field", "relation_m1"):
            values[item.column] = field.data
        elif item.kind == "relation_mn":
            mn.append((item, field.data))
    return values, mn
