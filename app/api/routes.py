"""REST/JSON API (``/api/v1``). Bearer-token auth (see the request_loader in
:mod:`app.helpers`); every request acts as the token's user, so the existing
permissions, row-ownership and soft-delete all apply via ``record_service``.
"""
from flask import Blueprint, g, jsonify, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import select

from .. import approvals, record_service, workflow
from ..db import SessionLocal, engine_for_table
from ..helpers import (
    current_user_id,
    readable_fields,
    table_readable,
    table_writable,
    writable_fields,
)
from ..metadata.models import MetaTable
from . import openapi, serialization

MAX_BULK = 1000  # cap on records per bulk request


def _hidden(session, table):
    """Columns the current user may not read (field-level permissions)."""
    return {f.phys_name for f in table.fields} - readable_fields(session, current_user, table)

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _err(status, message):
    return jsonify(error=message), status


@bp.errorhandler(405)
def _method_not_allowed(_e):
    return _err(405, "Method not allowed.")


@bp.before_request
def _require_token():
    if not current_user.is_authenticated:
        return _err(401, "Missing or invalid API token.")
    g.via_api = True  # lets the feed engine skip API-originated writes (loop guard)
    return None


def _s():
    return SessionLocal()


def _ctx():
    return current_user_id(), current_user.is_designer


def _table(session, name):
    return session.scalar(select(MetaTable).where(MetaTable.phys_name == name))


@bp.route("/")
def index():
    session = _s()
    tables = [t for t in session.scalars(select(MetaTable).order_by(MetaTable.label))
              if table_readable(session, current_user, t)]
    return jsonify(tables=[
        {"name": t.phys_name, "label": t.label,
         "endpoint": url_for("api.list_rows", table=t.phys_name)} for t in tables],
        openapi=url_for("api.openapi_json"), docs=url_for("api.docs"))


@bp.route("/openapi.json")
def openapi_json():
    """OpenAPI 3.0 spec for the tables the caller can read."""
    return jsonify(openapi.build_spec(_s(), current_user))


@bp.route("/docs")
def docs():
    """Self-hosted, no-CDN API reference rendered from the spec."""
    return render_template("api/docs.html", spec=openapi.build_spec(_s(), current_user))


@bp.route("/<table>", methods=["GET"])
def list_rows(table):
    session = _s()
    mt = _table(session, table)
    if not mt:
        return _err(404, "Unknown table.")
    engine = engine_for_table(mt)
    if not table_readable(session, current_user, mt):
        return _err(403, "No read access to this table.")
    user_id, is_designer = _ctx()
    field_names = {f.phys_name for f in mt.fields}
    filters = [{"col": k, "op": "eq", "value": v}
               for k, v in request.args.items() if k in field_names]
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(200, max(1, int(request.args.get("per_page", 50))))
    except (TypeError, ValueError):
        return _err(400, "page and per_page must be integers.")
    rows, total = record_service.list_records(
        engine, mt, user_id=user_id, is_designer=is_designer, filters=filters,
        sort=request.args.get("sort"), order=request.args.get("order", "asc"),
        page=page, per_page=per_page)
    hide = _hidden(session, mt)
    return jsonify(data=[serialization.serialize_row(r, hide) for r in rows],
                   page=page, per_page=per_page, total=total)


@bp.route("/<table>/fields", methods=["GET"])
def table_fields(table):
    """Field descriptors for a table — used by connectors to build field maps."""
    session = _s()
    mt = _table(session, table)
    if not mt:
        return _err(404, "Unknown table.")
    if not table_readable(session, current_user, mt):
        return _err(403, "No read access to this table.")
    readable = readable_fields(session, current_user, mt)
    writable = writable_fields(session, current_user, mt)
    return jsonify(table=mt.phys_name, fields=[
        {"name": f.phys_name, "label": f.label, "type": f.data_type,
         "writable": f.phys_name in writable}
        for f in mt.fields if f.phys_name in readable])


@bp.route("/<table>/<int:pk>", methods=["GET"])
def get_row(table, pk):
    session = _s()
    mt = _table(session, table)
    if not mt:
        return _err(404, "Unknown table.")
    engine = engine_for_table(mt)
    if not table_readable(session, current_user, mt):
        return _err(403, "No read access to this table.")
    user_id, is_designer = _ctx()
    row = record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer)
    if not row:
        return _err(404, "Record not found.")
    return jsonify(serialization.serialize_row(row, _hidden(session, mt)))


@bp.route("/<table>", methods=["POST"])
def create_row(table):
    session = _s()
    mt = _table(session, table)
    if not mt:
        return _err(404, "Unknown table.")
    engine = engine_for_table(mt)
    if not table_writable(session, current_user, mt):
        return _err(403, "No write access to this table.")
    try:
        values = serialization.deserialize(mt, request.get_json(silent=True), session, engine,
                                           partial=False, writable=writable_fields(session, current_user, mt))
    except serialization.ApiError as exc:
        return _err(400, str(exc))
    try:
        pk = record_service.create(session, engine, mt, values, current_user_id())
    except Exception as exc:  # noqa: BLE001 - surface DB errors (FK, unique, …)
        return _err(409, f"Could not create: {exc}")
    user_id, is_designer = _ctx()
    row = record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer)
    resp = jsonify(serialization.serialize_row(row, _hidden(session, mt)))
    resp.status_code = 201
    resp.headers["Location"] = url_for("api.get_row", table=table, pk=pk)
    return resp


# --------------------------------------------------------------------------- #
# Bulk operations (one HTTP call, many records; per-row error isolation)
# --------------------------------------------------------------------------- #
def _bulk_list(*keys):
    """Pull the record/id list from a bare array or an object with one of ``keys``."""
    body = request.get_json(silent=True)
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for k in keys:
            if isinstance(body.get(k), list):
                return body[k]
    return None


def _bulk_setup(table, key):
    """Resolve (mt, engine, items) or an error response for a bulk request."""
    session = _s()
    mt = _table(session, table)
    if not mt:
        return None, _err(404, "Unknown table.")
    if not table_writable(session, current_user, mt):
        return None, _err(403, "No write access to this table.")
    items = _bulk_list(key, "records", "ids")
    if items is None:
        return None, _err(400, f"Body must be a JSON array or {{\"{key}\": [...]}}.")
    if len(items) > MAX_BULK:
        return None, _err(400, f"Batch too large (max {MAX_BULK}).")
    return (mt, engine_for_table(mt), items), None


@bp.route("/<table>/bulk", methods=["POST"])
def bulk_create(table):
    ctx, err = _bulk_setup(table, "records")
    if err:
        return err
    mt, engine, records = ctx
    session = _s()
    writable = writable_fields(session, current_user, mt)
    created, errors = [], []
    for i, rec in enumerate(records):
        try:
            values = serialization.deserialize(mt, rec, session, engine,
                                               partial=False, writable=writable)
            created.append(record_service.create(session, engine, mt, values, current_user_id()))
        except Exception as exc:  # noqa: BLE001 - isolate one bad row; keep the batch going
            session.rollback()
            errors.append({"index": i, "error": str(exc)})
    return jsonify(created=created, errors=errors), (207 if errors else 201)


@bp.route("/<table>/bulk", methods=["PATCH"])
def bulk_update(table):
    ctx, err = _bulk_setup(table, "records")
    if err:
        return err
    mt, engine, records = ctx
    session = _s()
    user_id, is_designer = _ctx()
    writable = writable_fields(session, current_user, mt)
    updated, errors = [], []
    for i, rec in enumerate(records):
        try:
            pk = rec.get("id") if isinstance(rec, dict) else None
            if pk is None:
                raise serialization.ApiError("missing 'id'")
            old = record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer)
            if not old:
                raise serialization.ApiError(f"id {pk} not found")
            payload = {k: v for k, v in rec.items() if k != "id"}
            values = serialization.deserialize(mt, payload, session, engine,
                                               partial=True, writable=writable)
            diverted = approvals.plan_diversions(session, mt, old, values)
            workflow.check(session, mt, old, values, current_user)
            if values:
                record_service.update(session, engine, mt, pk, values, current_user_id())
            for d in diverted:
                approvals.request_transition(session, engine, mt, d["wf"], pk,
                                             d["frm"], d["to"], current_user)
            updated.append(pk)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append({"index": i, "error": str(exc)})
    return jsonify(updated=updated, errors=errors), (207 if errors else 200)


@bp.route("/<table>/bulk", methods=["DELETE"])
def bulk_delete(table):
    ctx, err = _bulk_setup(table, "ids")
    if err:
        return err
    mt, engine, ids = ctx
    session = _s()
    user_id, is_designer = _ctx()
    deleted, errors = [], []
    for i, pk in enumerate(ids):
        try:
            if not record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer):
                raise serialization.ApiError(f"id {pk} not found")
            record_service.remove(session, engine, mt, pk, current_user_id())
            deleted.append(pk)
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            errors.append({"index": i, "error": str(exc)})
    return jsonify(deleted=deleted, errors=errors), (207 if errors else 200)


@bp.route("/<table>/<int:pk>", methods=["PATCH", "PUT"])
def update_row(table, pk):
    session = _s()
    mt = _table(session, table)
    if not mt:
        return _err(404, "Unknown table.")
    engine = engine_for_table(mt)
    if not table_writable(session, current_user, mt):
        return _err(403, "No write access to this table.")
    user_id, is_designer = _ctx()
    old = record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer)
    if not old:
        return _err(404, "Record not found.")
    try:
        values = serialization.deserialize(mt, request.get_json(silent=True), session, engine,
                                           partial=True, writable=writable_fields(session, current_user, mt))
    except serialization.ApiError as exc:
        return _err(400, str(exc))
    diverted = approvals.plan_diversions(session, mt, old, values)
    try:
        workflow.check(session, mt, old, values, current_user)
    except workflow.WorkflowError as exc:
        return _err(409, str(exc))
    if values:
        try:
            record_service.update(session, engine, mt, pk, values, current_user_id())
        except Exception as exc:  # noqa: BLE001
            return _err(409, f"Could not update: {exc}")
    for d in diverted:
        approvals.request_transition(session, engine, mt, d["wf"], pk, d["frm"], d["to"], current_user)
    row = record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer)
    result = serialization.serialize_row(row, _hidden(session, mt))
    if diverted:
        result["_pending_approvals"] = [
            {"field": d["field"].phys_name, "from": d["frm"], "to": d["to"]} for d in diverted]
    return jsonify(result)


@bp.route("/<table>/<int:pk>", methods=["DELETE"])
def delete_row(table, pk):
    session = _s()
    mt = _table(session, table)
    if not mt:
        return _err(404, "Unknown table.")
    engine = engine_for_table(mt)
    if not table_writable(session, current_user, mt):
        return _err(403, "No write access to this table.")
    user_id, is_designer = _ctx()
    if not record_service.get_record(engine, mt, pk, user_id=user_id, is_designer=is_designer):
        return _err(404, "Record not found.")
    try:
        record_service.remove(session, engine, mt, pk, current_user_id())
    except Exception as exc:  # noqa: BLE001 - e.g. FK restrict
        return _err(409, f"Could not delete: {exc}")
    return "", 204
