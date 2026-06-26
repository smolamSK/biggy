# Biggy — Developer Guide

For the person **implementing / extending** Biggy. Explains the architecture, the
module map, and how to add common things. To deploy see
[Setup & operations](setup-and-operations.md); to author an app as JSON see
[Schema JSON format](schema-json-format.md).

---

## The core idea

Biggy is **metadata-driven**. Designer mode writes definitions into `app_meta_*`
tables **and** issues real DDL, so user data lives in genuine, query-able database
tables with real foreign keys — not in a generic "entity-attribute-value" blob. At
runtime, forms/lists/reports are generated from the metadata over those physical
tables.

Two consequences shape the whole codebase:

1. **Identifiers come only from metadata**, never from request strings, and DDL is
   emitted through SQLAlchemy objects — so dynamic table/column names can't be
   injected. All data *values* are bound parameters.
2. **Every physical table is reached through an engine chosen from metadata**
   (`db.engine_for_table`), which is what makes multiple data sources and adopted
   external tables work.

## Stack

Flask + Jinja + HTMX (server-rendered, no JS build), SQLAlchemy 2 (Core for dynamic
DDL/reflection, ORM for the `app_*` metadata), Alembic operations for `ALTER`,
PyMySQL, Flask-Login (token + session), Flask-WTF/WTForms, Markdown. Charts are
hand-rolled inline SVG (`static/charts.js`) — no chart library.

---

## Key pipelines (read these first)

### The write chokepoint — `record_service`
**All** writes (form, inline edit, REST API, bulk, kanban, webhooks, pulls) go
through `record_service.create` / `update` / `remove`. That's where triggers fire,
feeds push, **formula** fields recompute (and *ripple* to dependent rows), and audit
log / soft-delete / ownership stamps are applied. Add cross-cutting write behavior
here, not in the routes.

### The field-type pipeline
A field type is defined once and threaded through fixed touch-points:

```
metadata/field_types.py  (SCALAR_TYPES registry)
  → metadata/schema_service.sa_type_for_field()  (→ SQLAlchemy column type / DDL)
  → forms/builder._scalar_field()                (→ the WTForms field + widget)
  → importer.coerce_value()                      (→ parse/validate a raw value)
  → filters.filter_kind()                        (→ list/report filter behavior)
  → templates _macros.html typed_value           (→ how a value renders)
```

### DDL & portability — `metadata/schema_service` + `metadata/ddl`
Tables/columns are created and altered through SQLAlchemy + Alembic operations
(`ddl.operations`). On SQLite, `ALTER` uses Alembic's `batch_alter_table` (rebuild),
so the same designer actions work on MariaDB and SQLite. `ddl.fk_disabled` toggles
FK enforcement per dialect.

### Engines & multiple data sources — `db`
`engine_for(data_source)` / `engine_for_table(meta_table)` resolve and cache an
engine per source URL; the home database (where `app_*` lives) is the default
(`source_id = NULL`). Adopted tables (`MetaTable.managed = False`, see `adopt.py`)
are mapped, never issued DDL.

### Integrations
- **Out:** `connectors` (HTTP transport with a test-injectable loopback) + `feeds`.
- **In (push):** `hooks/` blueprint (`POST /hooks/<token>`).
- **In (pull):** `pull` (poll a peer/REST source on a schedule).
- **Schedule:** `scheduler.run_due` runs due scheduled triggers + feeds + pulls +
  report digests; driven by `flask run-jobs` or the in-process ticker.

### Reporting, dashboards, API
`reporting` (group-by + `chart_data`) feeds `static/charts.js` and `dashboards`
(chart/KPI/list/text tiles). `api/` is the REST surface: `routes` (CRUD + bulk),
`serialization`, `tokens`, and `openapi` (auto-generated spec at `/api/v1/openapi.json`,
docs at `/api/v1/docs`). `schema_io` exports/imports the whole model as JSON
(see [Schema JSON format](schema-json-format.md)).

---

## Module map

```
app/
  __init__.py            app factory: blueprints, CSRF exempt (api/hooks), CLI, scheduler ticker
  config.py db.py        env config; engine/session registry (multi-source)
  identifiers.py         identifier validation + reserved names (injection safety)
  helpers.py             auth (token request_loader), permissions, menu tree
  extensions.py          login_manager, csrf

  metadata/
    models.py            all app_* ORM models (the metadata + support tables)
    field_types.py       SCALAR_TYPES registry (the field-type source of truth)
    schema_service.py    DDL: create/alter tables + columns + junctions; type mapping
    ddl.py               Alembic operations + per-dialect FK toggling (portability)

  record_service.py      THE write chokepoint (triggers/feeds/formula/audit)
  data_service.py        generic CRUD/search/aggregate over physical tables (PK-agnostic)
  formula.py             safe AST formula evaluator + lookup()/rollup() + ripple
  importer.py filters.py coerce values; list/report filter clauses
  workflow.py triggers.py  status graphs; event rules + notifications
  connectors.py feeds.py   outbound HTTP + feed engine
  pull.py scheduler.py     inbound polling; the job runner + ticker
  reporting.py dashboards.py  group-by/charts; dashboard tiles
  schema_io.py data_io.py  schema/data JSON export-import
  adopt.py                 map pre-existing external tables (managed=False)
  file_store.py list_export.py sql_console.py examples.py help.py

  forms/  builder.py (dynamic forms from metadata)  admin_forms.py (fixed WTForms)
  api/    routes.py serialization.py tokens.py openapi.py
  auth/ core/ designer/ user/ hooks/   (Flask blueprints; routes.py each)
  templates/ static/
tests/   unit + integration (biggy_test); conftest.py fixtures
```

### Data model (`app_*` tables)
Metadata: `app_meta_table/field/relation/form/form_field/menu`, `app_workflow`,
`app_trigger_rule`, `app_unique`, `app_sequence`, `app_data_source`. Access/identity:
`app_user`, `app_role`, `app_meta_permission`, `app_field_permission`, `app_api_token`.
Runtime/UX: `app_audit_log`, `app_attachment`, `app_saved_view`, `app_notification`,
`app_report`, `app_dashboard`, `app_dashboard_widget`. Integrations: `app_connection`,
`app_feed`, `app_webhook`, `app_pull_source`. (User data tables are separate, created
by the designer.)

---

## Recipes

### Add a field type
1. Register it in `metadata/field_types.SCALAR_TYPES`.
2. Map it to a column type in `schema_service.sa_type_for_field`.
3. Build its form field/widget in `forms/builder._scalar_field`.
4. Parse/validate it in `importer.coerce_value` (+ a `filters.filter_kind` entry).
5. Render it in the `typed_value` macro (`templates/_macros.html`).
6. Add it to the OpenAPI map in `api/openapi.py`; document it in
   `docs/schema-json-format.md`; add a test.

### Add a write-time behavior
Hook it into `record_service` (create/update/remove) so it applies to every write
path uniformly.

### Add a new metadata column
Add the `mapped_column` to the model, and a backfill entry in
`schema_service._META_ADDITIONS` (so existing databases get it on boot), and the
column to the relevant `schema_io._*_COLS` for round-trip.

### Add an integration / blueprint
Mirror an existing one: a module (engine) + a blueprint (`routes.py`) registered in
`app/__init__.py`; reuse `connectors.TRANSPORT` (loopback-testable), `record_service`
for writes, and `schema_io` for round-trip. Public token-auth blueprints
(`api`, `hooks`) are CSRF-exempt and allowed through the bootstrap guard.

### Add a dashboard widget kind
Extend `dashboards.render` (a new `kind` branch) + `DashboardWidget` + the
`dashboard_view.html` tile rendering.

---

## Security model

- **No SQL injection of identifiers:** table/column names are validated
  (`identifiers.validate_identifier`, `^[a-z][a-z0-9_]*$`, reserved prefixes blocked)
  and only ever emitted via SQLAlchemy objects; values are bound parameters.
- **CSRF** on all browser forms; the token-auth `api`/`hooks` blueprints are exempt.
- **Roles** (`designer`/`user` + custom), per-form and per-field permissions, row
  ownership, soft-delete. API requests act *as* the token's user, so the same checks
  apply.

## Testing

- `tests/` mixes pure unit tests (no DB) with integration tests against `biggy_test`
  (skipped if unavailable); a second `biggy_test2` and a temp SQLite source exercise
  multi-source / portability. Fixtures in `conftest.py`.
- The connectors **loopback transport** (`connectors.set_transport`) routes outbound
  HTTP through a test client, so feeds/pull/chaining are tested without sockets.
- `record_service`, `schema_io`, and the field-type pipeline are the highest-leverage
  things to test when you change them.

```bash
.venv/bin/python -m pytest -q
```
