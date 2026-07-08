"""Auto-generate an OpenAPI 3.0 document from the Biggy table metadata.

Built per-request and scoped to the tables the caller can read (so the spec only
advertises what they may use). Mirrors the real ``/api/v1`` routes in
:mod:`app.api.routes` — list/get/create/update/delete plus the bulk endpoints.
"""
import json

from sqlalchemy import select

from .. import __version__, helpers
from ..metadata.field_types import FILE_TYPES, RELATION_TYPE
from ..metadata.models import MetaTable

# data_type → JSON-schema fragment (relation/enum handled specially below)
_TYPE_MAP = {
    "string": {"type": "string"}, "text": {"type": "string"},
    "markdown": {"type": "string"},
    "integer": {"type": "integer"}, "bigint": {"type": "integer", "format": "int64"},
    "float": {"type": "number"}, "decimal": {"type": "number"},
    "currency": {"type": "number"}, "percent": {"type": "number"},
    "boolean": {"type": "boolean"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "time": {"type": "string"},
    "email": {"type": "string", "format": "email"},
    "url": {"type": "string", "format": "uri"}, "phone": {"type": "string"},
    "json": {"type": "object"},
    "tags": {"type": "array", "items": {"type": "string"}},
    "user": {"type": "integer", "description": "App user id (assignee)"},
    "autonumber": {"type": "string"},
}
_GENERATED = {"formula", "autonumber"}


def _field_schema(field):
    if field.data_type == RELATION_TYPE:
        s = {"type": "integer", "description": "Related record id"}
    elif field.data_type == "enum":
        s = {"type": "string"}
        try:
            opts = json.loads(field.enum_options or "[]")
        except ValueError:
            opts = []
        if opts:
            s["enum"] = opts
    else:
        s = dict(_TYPE_MAP.get(field.data_type, {"type": "string"}))
    if field.data_type in _GENERATED:
        s["readOnly"] = True
    return s


def _table_schema(table):
    props, required = {}, []
    for f in sorted(table.fields, key=lambda x: x.position):
        if f.data_type in FILE_TYPES:                  # uploads are virtual — not over JSON
            continue
        props[f.phys_name] = _field_schema(f)
        if (not f.nullable and not f.default_value and f.data_type not in _GENERATED
                and f.phys_name != table.pk_col):
            required.append(f.phys_name)
    props.setdefault(table.pk_col, {"type": "integer", "readOnly": True})
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _json(schema):
    return {"content": {"application/json": {"schema": schema}}}


def _resp(schema, desc="OK"):
    return {"description": desc, **_json(schema)}


def _err_resp():
    return _resp({"$ref": "#/components/schemas/Error"}, "Error")


def _list_params(table):
    params = [
        {"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}},
        {"name": "per_page", "in": "query",
         "schema": {"type": "integer", "default": 50, "maximum": 200}},
        {"name": "sort", "in": "query", "schema": {"type": "string"}},
        {"name": "order", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}},
    ]
    for f in table.fields:
        if f.data_type not in FILE_TYPES:
            params.append({"name": f.phys_name, "in": "query", "schema": {"type": "string"},
                           "description": f"Filter by {f.label} (equals)"})
    return params


def _bulk_result():
    return {"type": "object", "properties": {
        "created": {"type": "array", "items": {"type": "integer"}},
        "updated": {"type": "array", "items": {"type": "integer"}},
        "deleted": {"type": "array", "items": {"type": "integer"}},
        "errors": {"type": "array", "items": {"type": "object"}}}}


def _paths(tables):
    paths = {}
    for t in tables:
        ref = {"$ref": f"#/components/schemas/{t.phys_name}"}
        tag, n = t.label, t.phys_name
        paths[f"/{n}"] = {
            "get": {"tags": [tag], "summary": f"List {t.label}",
                    "parameters": _list_params(t),
                    "responses": {"200": _resp({"type": "object", "properties": {
                        "data": {"type": "array", "items": ref}, "page": {"type": "integer"},
                        "per_page": {"type": "integer"}, "total": {"type": "integer"}}})}},
            "post": {"tags": [tag], "summary": f"Create {t.label}",
                     "requestBody": _json(ref),
                     "responses": {"201": _resp(ref, "Created"), "400": _err_resp()}},
        }
        paths[f"/{n}/{{id}}"] = {
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "integer"}}],
            "get": {"tags": [tag], "summary": f"Get {t.label}",
                    "responses": {"200": _resp(ref), "404": _err_resp()}},
            "patch": {"tags": [tag], "summary": f"Update {t.label}", "requestBody": _json(ref),
                      "responses": {"200": _resp(ref), "404": _err_resp()}},
            "delete": {"tags": [tag], "summary": f"Delete {t.label}",
                       "responses": {"204": {"description": "Deleted"}, "404": _err_resp()}},
        }
        paths[f"/{n}/bulk"] = {
            "post": {"tags": [tag], "summary": f"Bulk create {t.label}",
                     "requestBody": _json({"type": "object", "properties": {
                         "records": {"type": "array", "items": ref}}}),
                     "responses": {"201": _resp(_bulk_result()), "207": _resp(_bulk_result(),
                                                                             "Partial success")}},
            "patch": {"tags": [tag], "summary": f"Bulk update {t.label}",
                      "requestBody": _json({"type": "array", "items": ref}),
                      "responses": {"200": _resp(_bulk_result())}},
            "delete": {"tags": [tag], "summary": f"Bulk delete {t.label}",
                       "requestBody": _json({"type": "object", "properties": {
                           "ids": {"type": "array", "items": {"type": "integer"}}}}),
                       "responses": {"200": _resp(_bulk_result())}},
        }
    return paths


def build_spec(session, user):
    """Return the OpenAPI 3.0 dict for the tables ``user`` can read."""
    tables = [t for t in session.scalars(select(MetaTable).order_by(MetaTable.label))
              if helpers.table_readable(session, user, t)]
    schemas = {t.phys_name: _table_schema(t) for t in tables}
    schemas["Error"] = {"type": "object", "properties": {"error": {"type": "string"}}}
    return {
        "openapi": "3.0.3",
        "info": {"title": "Biggy API", "version": __version__,
                 "description": "Auto-generated from the Biggy table metadata. "
                                "Authenticate with `Authorization: Bearer <token>`."},
        "servers": [{"url": "/api/v1"}],
        "security": [{"bearerAuth": []}],
        "tags": [{"name": t.label} for t in tables],
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": schemas,
        },
        "paths": _paths(tables),
    }
