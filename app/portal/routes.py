"""Customer portal — the third mode, next to Designer and User.

External ``portal``-role accounts get a deliberately narrow surface: submit a
request/incident from the service catalog, see **their own** tickets, and talk
to staff through the record conversation (public comments only — internal work
notes never appear here). The access contract is simple: a form being in the
catalog *is* the grant, and only tables with owner stamps (``track_audit`` or
``row_owned``) are usable, because everything is scoped to ``created_by``.
Customers never edit fields — staff does that in User mode.
"""
import json
import os

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import select

from .. import approvals, comments, data_service, file_store, record_service, workflow
from ..db import SessionLocal, engine_for_table
from ..forms.builder import build_form, display_field_name
from ..helpers import current_user_id
from ..importer import coerce_value
from ..metadata.models import Attachment, MetaField, MetaForm, MetaTable, Notification
from ..user.routes import (
    _apply_workflow_choices,
    _collect,
    _save_attachments,
    _view_items,
)

bp = Blueprint("portal", __name__, url_prefix="/portal")


@bp.before_request
@login_required
def _guard():
    if not (current_user.is_portal or current_user.is_designer):
        return redirect(url_for("core.index"))


def _s():
    return SessionLocal()


def _catalog_forms(session):
    """Catalog forms usable in the portal (owner-stamped tables only)."""
    return [f for f in session.scalars(
        select(MetaForm).where(MetaForm.in_catalog.is_(True)).order_by(MetaForm.title))
        if f.purpose != "view" and (f.table.track_audit or f.table.row_owned)]


def _get_catalog_form(session, form_id):
    mf = session.get(MetaForm, form_id)
    if not mf or not mf.in_catalog or mf.purpose == "view" \
            or not (mf.table.track_audit or mf.table.row_owned):
        abort(404)
    return mf


def _own_row(session, table, pk):
    """The record, if it belongs to the caller (designers may preview any)."""
    row = record_service.get_record(engine_for_table(table), table, pk,
                                    user_id=current_user_id(), is_designer=True)
    if not row:
        abort(404)
    if not current_user.is_designer and row.get("created_by") != current_user_id():
        abort(404)
    return row


def _label(session, table, row, pk):
    val = row.get(display_field_name(session, table))
    return str(val) if val not in (None, "") else f"#{pk}"


def _status_field(table):
    return next((f for f in table.fields if f.data_type == "enum"), None)


def _close_allowed(session, mf, table, row):
    """The close-state value when the customer may close this ticket now, else None.

    Honors the designer's workflow exactly like a staff edit would: the
    current → close transition must exist, be open to this user's role, and not
    be gated behind an approval step.
    """
    close, status_f = mf.portal_close_state, _status_field(table)
    if not close or status_f is None:
        return None
    if close not in json.loads(status_f.enum_options or "[]"):
        return None                                   # stale setting (option renamed)
    current = row.get(status_f.phys_name)
    if current == close:
        return None                                   # already closed
    wf = workflow.for_table(session, table.id).get(status_f.id)
    if wf is not None:
        if close not in workflow.allowed_choices(wf, current, current_user):
            return None
        probe = {status_f.phys_name: close}           # approval-gated? then no button
        if approvals.plan_diversions(session, table, row, probe):
            return None
    return close


def _my_tickets(session, forms):
    uid = current_user_id()
    items, seen = [], set()
    for f in forms:
        t = f.table
        if t.id in seen:
            continue
        seen.add(t.id)
        status_f = _status_field(t)
        colors = json.loads(status_f.enum_colors) \
            if status_f is not None and status_f.enum_colors else None
        rows, _total = record_service.list_records(
            engine_for_table(t), t, user_id=uid, is_designer=True,
            filters=[{"col": "created_by", "op": "eq", "value": uid}],
            sort="created_at", order="desc", per_page=50)
        disp = display_field_name(session, t)
        for r in rows:
            items.append({"table": t, "pk": r[t.pk_col],
                          "label": str(r.get(disp)) if r.get(disp) not in (None, "")
                          else f"#{r[t.pk_col]}",
                          "status": r.get(status_f.phys_name) if status_f else None,
                          "colors": colors, "created_at": r.get("created_at")})
    items.sort(key=lambda x: (x["created_at"] is None, x["created_at"]), reverse=True)
    return items


@bp.route("/")
def home():
    session = _s()
    forms = _catalog_forms(session)
    groups = {}
    for f in forms:
        groups.setdefault(f.catalog_group or "General", []).append(f)
    return render_template("portal/home.html", groups=dict(sorted(groups.items())),
                           tickets=_my_tickets(session, forms))


@bp.route("/new/<int:form_id>", methods=["GET", "POST"])
def new(form_id):
    session = _s()
    mf = _get_catalog_form(session, form_id)
    engine = engine_for_table(mf.table)
    built = build_form(mf, session, engine, current_user)
    uid = current_user_id()

    if request.method == "POST":
        form = built.form_class()
        if form.validate():
            values, mn = _collect(built, form)
            pk = record_service.create(session, engine, mf.table, values, uid)
            for item, ids in mn:
                data_service.set_links(engine, item.junction, item.this_col, pk,
                                       item.other_col, ids)
            for item in built.items:
                if item.kind == "file":
                    _save_attachments(session, item, pk)
            flash("Request submitted — you can follow it here.", "success")
            return redirect(url_for("portal.ticket", table_id=mf.table_id, pk=pk))
    else:
        defaults = {}
        _tokens = ("now", "today", "current_user", "me")
        for it in built.items:
            if (it.kind == "field" and it.meta.default_value not in (None, "")
                    and it.meta.default_value.strip().lower() not in _tokens):
                try:
                    defaults[it.name] = coerce_value(it.meta, it.meta.default_value)
                except ValueError:
                    pass
        form = built.form_class(data=defaults)
    _apply_workflow_choices(session, engine, mf, built, form, None, uid, False)
    return render_template("portal/new.html", mf=mf, form=form, items=built.items)


@bp.route("/ticket/<int:table_id>/<pk>")
def ticket(table_id, pk):
    session = _s()
    table = session.get(MetaTable, table_id)
    mf = next((f for f in _catalog_forms(session) if f.table_id == table_id), None)
    if not table or mf is None:
        abort(404)
    row = _own_row(session, table, pk)
    engine = engine_for_table(table)
    built = build_form(mf, session, engine, current_user)
    status_f = _status_field(table)
    return render_template(
        "portal/ticket.html", table=table, pk=pk, label=_label(session, table, row, pk),
        items=_view_items(session, engine, mf, built, row),
        thread=comments.list_for(session, table.phys_name, pk, include_internal=False),
        status=row.get(status_f.phys_name) if status_f else None,
        status_colors=json.loads(status_f.enum_colors)
        if status_f is not None and status_f.enum_colors else None,
        created_at=row.get("created_at"),
        can_close=_close_allowed(session, mf, table, row))


@bp.route("/ticket/<int:table_id>/<pk>/comment", methods=["POST"])
def ticket_comment(table_id, pk):
    session = _s()
    table = session.get(MetaTable, table_id)
    if not table or not any(f.table_id == table_id for f in _catalog_forms(session)):
        abort(404)
    row = _own_row(session, table, pk)
    try:
        comments.add(session, table.phys_name, pk, current_user,
                     request.form.get("body"), internal=False, row=row,
                     record_label=f"{table.label}: {_label(session, table, row, pk)}")
        flash("Comment added.", "success")
    except ValueError as exc:
        flash(str(exc), "warning")
    return redirect(url_for("portal.ticket", table_id=table_id, pk=pk))


@bp.route("/ticket/<int:table_id>/<pk>/close", methods=["POST"])
def ticket_close(table_id, pk):
    """Customer closes their own ticket: a real status update through the
    write chokepoint (audit, triggers, SLA stop) + a public comment."""
    session = _s()
    table = session.get(MetaTable, table_id)
    mf = next((f for f in _catalog_forms(session) if f.table_id == table_id), None)
    if not table or mf is None:
        abort(404)
    row = _own_row(session, table, pk)
    close = _close_allowed(session, mf, table, row)
    if not close:
        flash("This ticket can't be closed from the portal right now.", "warning")
        return redirect(url_for("portal.ticket", table_id=table_id, pk=pk))

    status_f = _status_field(table)
    values = {status_f.phys_name: close}
    try:
        workflow.check(session, table, row, values, current_user)
    except workflow.WorkflowError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("portal.ticket", table_id=table_id, pk=pk))
    record_service.update(session, engine_for_table(table), table, pk, values,
                          current_user_id())

    reason = (request.form.get("reason") or "").strip()
    comments.add(session, table.phys_name, pk, current_user,
                 f"Closed by customer: {reason}" if reason else "Closed by customer.",
                 internal=False, row=row,
                 record_label=f"{table.label}: {_label(session, table, row, pk)}")
    flash("Ticket closed — thank you!", "success")
    return redirect(url_for("portal.ticket", table_id=table_id, pk=pk))


@bp.route("/attachment/<int:att_id>")
def attachment(att_id):
    """Serve a file from the caller's own catalog record (owner-checked)."""
    session = _s()
    att = session.get(Attachment, att_id)
    field = session.get(MetaField, att.field_id) if att else None
    table = session.get(MetaTable, field.table_id) if field else None
    if not att or not field or not table:
        abort(404)
    if not any(f.table_id == table.id for f in _catalog_forms(session)):
        abort(404)
    _own_row(session, table, att.row_pk)
    path = file_store.abs_path(att.field_id, att.stored_name)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype=att.content_type or "application/octet-stream",
                     download_name=att.original_name, as_attachment=False)


@bp.route("/notifications", methods=["GET", "POST"])
def notifications():
    session = _s()
    uid = current_user_id()
    if request.method == "POST":
        for n in session.scalars(select(Notification).where(
                Notification.user_id == uid, Notification.channel == "in_app",
                Notification.status == "unread")):
            n.status = "read"
        session.commit()
        return redirect(url_for("portal.notifications"))
    items = session.scalars(select(Notification).where(
        Notification.user_id == uid, Notification.channel == "in_app")
        .order_by(Notification.id.desc()).limit(100)).all()
    # link each notification to its ticket when it points at a catalog table
    tables = {f.table.phys_name: f.table_id for f in _catalog_forms(session)}
    links = {n.id: url_for("portal.ticket", table_id=tables[n.table_phys], pk=n.row_pk)
             for n in items
             if n.table_phys in tables and n.row_pk is not None}
    return render_template("portal/notifications.html", items=items, links=links)
