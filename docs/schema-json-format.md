# Biggy schema-import JSON format

This document explains how to author a **Biggy schema file** — a single JSON
document that defines an entire application (tables, fields, relations, forms,
menus, dashboards, workflows, triggers, and integrations). It is written for both
humans and LLMs: read [§Instructions for an LLM](#instructions-for-an-llm) first if
you are generating a file automatically.

A ready-to-edit, fully-worked file lives next to this one:
**[`schema-reference.example.json`](schema-reference.example.json)** — it exercises
every section and imports cleanly. Start from it.

---

## 1. What the file is and how it loads

- A schema file is JSON with a top-level `"version": 1` and one array (or object)
  per **section** (`tables`, `fields`, …). Every section is optional except that a
  useful app needs at least `tables` + `fields`.
- Load it in the app at **Designer → Backup → Import schema** (tick *Replace
  existing* to wipe the current model first). Programmatically it is consumed by
  `app.schema_io.import_schema(session, engine, data, replace=...)`.
- It is **schema only** — no row data. Sample rows are a *separate* file in the
  data format `{"tables": {"<table_name>": [ {row}, ... ]}}`, imported at
  **Designer → Backup → Import data**.
- On **export** (the inverse), secrets are stripped and runtime state is reset — see
  [§7 Secrets & runtime state](#7-secrets--runtime-state). Personal dashboards and
  saved reports are *not* exported.

Minimal valid file:

```json
{
  "version": 1,
  "tables": [{"id": 1, "phys_name": "task", "label": "Task", "display_field_id": 1}],
  "fields": [
    {"id": 1, "table_id": 1, "phys_name": "title", "label": "Title",
     "data_type": "string", "length": 200, "nullable": false}
  ]
}
```

---

## 2. The id-reference rule (most important)

Every object has an integer **`id`**. **These ids are local to the file** — they are
*not* database ids. You choose them; they only need to be **unique within their
section** and used consistently when one object refers to another. On import, Biggy
assigns fresh database ids and **remaps every reference** for you.

So the workflow is: *give each table/field/form/etc. an id, then point at those ids
from other sections.* Order inside the file does not matter (the importer resolves
references across the whole document).

Every reference field and what it points at:

| Field (in section) | Refers to an `id` in |
|---|---|
| `fields[].table_id` | `tables` |
| `fields[].related_table_id` (relation type) | `tables` |
| `tables[].display_field_id` | `fields` (of that table) |
| `tables[].source_id` | `data_sources` |
| `relations[].from_table_id`, `to_table_id` | `tables` |
| `relations[].from_field_id` (m1 only) | `fields` (the relation-type field) |
| `relations[].to_display_field_ids`, `from_display_field_ids` | `fields` (JSON list) |
| `forms[].table_id` | `tables` |
| `form_fields[].form_id` | `forms` |
| `form_fields[].field_id` | `fields` |
| `form_fields[].relation_id` | `relations` |
| `menus[].parent_id` | `menus` |
| `menus[].target_form_id` / `target_table_id` / `target_dashboard_id` | `forms` / `tables` / `dashboards` |
| `permissions[].form_id` | `forms` |
| `field_permissions[].field_id` | `fields` |
| `composite_uniques[].table_id`, `field_ids` | `tables`, `fields` (JSON list) |
| `workflows[].table_id`, `field_id` | `tables`, `fields` (an enum field) |
| `trigger_rules[].table_id`, `field_id`, `cond_field_id`, `set_field_id` | `tables`, `fields` |
| `feeds[].source_table_id`, `connection_id`, `field_id`, `cond_field_id` | `tables`, `connections`, `fields` |
| `webhooks[].target_table_id` | `tables` |
| `pull_sources[].target_table_id`, `connection_id` | `tables`, `connections` |
| `dashboard_widgets[].dashboard_id`, `table_id` | `dashboards`, `tables` |
| `sequences[].field_id` | `fields` (an `autonumber` field) |

> User accounts are **not** part of the schema. Reference fields that point at a user
> (`trigger_rules[].notify_user_id`, `webhooks[].user_id`, `pull_sources[].user_id`)
> are kept as-is on import (best-effort) — usually set them to `null`.

---

## 3. Nested values are JSON-encoded **strings** (top gotcha)

Several fields hold structured data, but because they are stored as text columns
their value in the file is a **string that contains JSON**, *not* a nested
array/object. Encode the inner JSON and put it in a string.

| Field | Example value (note the outer quotes) |
|---|---|
| `fields[].enum_options` (enum/tags) | `"[\"new\", \"open\", \"done\"]"` |
| `fields[].enum_colors` (enum, optional) | `"{\"open\": \"amber\", \"done\": \"green\"}"` |
| `relations[].to_display_field_ids` | `"[10]"` |
| `composite_uniques[].field_ids` | `"[10, 12]"` |
| `feeds[].field_map`, `webhooks[].field_map`, `pull_sources[].field_map` | `"[{\"target\": \"name\", \"source\": \"full_name\"}]"` |
| `workflows[].transitions` | `"[{\"from\": \"new\", \"to\": \"done\", \"roles\": []}]"` |
| `workflows[].layout` | `"{}"` |
| `pull_sources[].config` | `"{\"pagination\": {\"style\": \"page\"}}"` |

Everything else (`menus`, `forms`, `dashboards`, …) uses **plain JSON** values.

---

## 4. Relations (read this before adding links between tables)

**Many-to-one (`m1`)** — e.g. each *order* belongs to one *customer*:

1. Add a **field** of `data_type: "relation"` on the *child* table, with
   `related_table_id` set to the parent table's id. Give it an id.
2. Add a `relations` entry with `kind: "m1"`, `from_table_id` = child table,
   `to_table_id` = parent table, and `from_field_id` = the relation field's id.

The importer turns that field into the real foreign-key column — **do not** add a
scalar column for it yourself, and don't forget the `relations` entry (a relation
field without one is ignored).

**Many-to-many (`mn`)** — e.g. *customer* ↔ *tag*:

- Add **only** a `relations` entry with `kind: "mn"`, `from_table_id` and
  `to_table_id`. **No field is needed.** The importer creates a junction table
  automatically.

`on_delete` may be `"SET NULL"`, `"CASCADE"`, or `"RESTRICT"`. Both related tables
must live in the same data source.

---

## 5. Field types

`fields[].data_type` is one of:

| `data_type` | JSON value of a row | Notes / extra keys |
|---|---|---|
| `string` | text | `length` (e.g. 255); validators `min_length`/`max_length`/`pattern` |
| `text` | long text | multi-line |
| `markdown` | markdown text | rendered as HTML on record pages (raw HTML neutralized) |
| `integer` | number | validators `min_value`/`max_value` (as strings) |
| `bigint` | number | 64-bit |
| `float` | number | |
| `decimal` | number | `precision`, `scale` |
| `currency` | number | shown with the currency symbol |
| `percent` | number | |
| `boolean` | `true`/`false` | |
| `date` | `"2026-01-31"` | |
| `datetime` | `"2026-01-31T14:00:00"` | ISO 8601 |
| `time` | `"14:00"` | |
| `email` | text | format-validated |
| `url` | text | http/https |
| `phone` | text | |
| `json` | object/array (in **data** rows) | arbitrary JSON value |
| `enum` | one of the options | `enum_options` = JSON-string list (§3) |
| `tags` | list of options | `enum_options` = JSON-string list (§3) |
| `user` | app-account id (assignee) | rendered as the username; `default_value` `"me"` assigns the creator; filterable by `me` |
| `company` | company id (tenant) | scopes visibility to the user's company subtree; companies themselves are instance data (Admin → Companies), not exported |
| `autonumber` | generated | `default_value` = prefix (e.g. `"INV-"`); pair with a `sequences` entry |
| `formula` | computed (read-only) | `formula` = expression, `result_type` = a scalar type |
| `relation` | FK id | virtual; defined via `relations` (§4) — needs `related_table_id` |
| `file`, `image` | upload | **virtual** — no DB column; not part of schema data |

Common per-field keys: `nullable` (default `true`; set `false` to require),
`default_value` (string), `is_unique`, `position` (display order), `label`.

---

## 6. Section reference

Each row's `id` is file-local (§2). Below, **req** = practically required to be
useful; omitted keys take sensible defaults.

### `data_sources` — other databases to map (optional)
`id`, `name`, `driver` (e.g. `mysql+pymysql`, `sqlite`), `host`, `port`, `username`,
`password`, `database`, `active`. A table maps to a source via `tables[].source_id`.
Leave this out for an all-in-one app (the home database is implicit, `source_id:
null`).

### `tables`
`id`, **`phys_name`** (the real table name — see [§8 identifiers](#8-rules--gotchas)),
`label`, `description`, `display_field_id` (which field labels a row), `track_audit`
(keep created/updated stamps + an audit log), `soft_delete` (trash instead of hard
delete), `row_owned` (users see only their own rows), `managed` (true = Biggy owns
the table and issues DDL; false = an *adopted* external table), `source_id`,
**`pk_col`** (default `"id"` = an auto-increment integer key; set to a field's
`phys_name` to use a natural key — that field must then exist and be required).

### `fields`
See [§5](#5-field-types). `id`, `table_id`, `phys_name`, `label`, `data_type`, plus
type-specific keys.

### `relations`
See [§4](#4-relations). `id`, `name`, `kind` (`m1`|`mn`), `from_table_id`,
`to_table_id`, `from_field_id` (m1), `junction_phys_name` (mn — usually `null`, auto),
`on_delete`, `to_display_field_ids`, `from_display_field_ids`.

### `forms`
`id`, `table_id`, `name` (identifier), `title`, `description`, `purpose`
(`"data"` = create/edit, `"view"` = read-only view). Optional service catalog:
`in_catalog` (bool — show as a request card on `/u/catalog`) + `catalog_group`.
Optional list defaults: `default_sort` (a physical column of the table),
`default_order` (`"asc"`/`"desc"`), `default_per_page` (25/50/100) — how the
form's list opens when the visitor hasn't chosen a sort or page size.

### `form_fields` — which fields appear on a form, in order
`id`, `form_id`, `kind` (`"field"` = a table field via `field_id`; `"relation"` = a
many-to-many related list via `relation_id`), `field_id`, `relation_id`,
`label_override`, `widget`, `required`, `readonly`, `help_text`, `position`,
`parent_field_id` + `filter_field_id` (dependent dropdowns).

### `menus` — the left-nav
`id`, `parent_id` (a `"group"` menu, or `null` for top level), `label`, `kind`
(`"group"` heading, `"form"`, `"list"` view, `"dashboard"`), `target_form_id` /
`target_table_id` / `target_dashboard_id` (per kind), `position`, `icon`.

### `roles`, `permissions`, `field_permissions` — access control
- `roles`: `name`, `label`, `builtin`. (Built-ins `designer`/`user` always exist.)
- `permissions`: `id`, `role`, `form_id`, `access` (`"none"`|`"read"`|`"write"`).
- `field_permissions`: `role`, `field_id`, `access` — field-level overrides.

### `composite_uniques` — multi-column unique constraints
`table_id`, `name`, `field_ids` (JSON-string list, e.g. `"[10, 12]"`).

### `workflows` — a state machine on an enum field
`id`, `table_id`, `field_id` (an `enum` field), `initial_state`, `transitions`
(JSON-string list of `{"from","to","roles":[role names]}`), `layout` (JSON string,
`"{}"` is fine). A transition that has `approval_steps` (below) is **held** for
sign-off instead of applying immediately.

### `trigger_rules` — when X happens, do Y
`id`, `table_id`, `name`, `active`, `event` (`create`|`update`|`transition`|`delete`|
`scheduled`), transition match (`field_id`,`from_state`,`to_state`), optional
condition (`cond_field_id`,`cond_op`,`cond_value`), and actions: `in_app`+`notify_target`
(`actor`|`owner`|`user`)+`notify_user_id`+`message`; `email_to`/`email_subject`/
`email_body`; `webhook_url` (+ `webhook_format`: `"json"` full payload |
`"text"` → `{"text": message}` for Slack/Teams); `set_field_id`+`set_value`;
`create_table_id`+`create_field_map` (JSON-string list of `{"target","source"}`
with `{field}` templates — creates a record in another table, depth-capped). For `event:"scheduled"`,
set `schedule_minutes` (and use a condition + a `set_field` so a row isn't acted on
twice). `cond_op` ∈ `eq, ne, contains, not_contains, starts_with, ends_with, gt,
gte, lt, lte, empty, not_empty, is_true, is_false`. Messages support `{field}`
placeholders.

### `sla_policies` — a service-level target on a table (ITSM)
`id`, `table_id`, `name`, `active`, `target_minutes` (the goal). A per-record clock
starts/pauses/stops from a status field: `status_field_id` (an `enum` field) plus
comma-separated `start_states` / `pause_states` / `stop_states` (state names, *not*
JSON); `start_on_create` (bool). The clock measures 24×7 time (paused spans excluded);
`warn_minutes` is the "due soon" threshold (blank = the `SLA_DEFAULT_WARN_MINUTES`
default). The live state and deadline are **written back** to `state_field_id` (an
`enum` field carrying `on_track`/`due_soon`/`paused`/`met`/`breached`) and
`due_field_id` (a `datetime` field), so they show in lists/reports and can drive
triggers. Optional applies-when: `cond_field_id`/`cond_op`/`cond_value` (same operators
as a trigger). On breach: `breach_in_app` + `breach_notify_target`
(`owner`|`actor`|`user`) + `breach_notify_user_id` + `breach_message`;
`breach_email_to`/`breach_email_subject`/`breach_email_body`;
`breach_set_field_id` + `breach_set_value`. Optional `escalations` (JSON-string list
of levels `{"after_minutes", "notify_target"|"notify_user_id", "email_to", "message"}`,
fired in order as the breach ages). (Per-record **clocks** are runtime — not exported.)

### `approval_steps` — multi-step sign-off on a workflow transition
`id`, `workflow_id` (a `workflows[].id`), `from_state`, `to_state` (the transition this
governs), `position` (steps with the **same** position run in parallel — all must
approve; **different** positions run in sequence), `name`, and an approver: either
`approver_role` (any user with that role — see `roles`) **or** `approver_user_id`.
A transition with any steps is held for sign-off; full approval applies it, any
rejection cancels it. (The running **requests/actions** are runtime — not exported.)

### `connections`, `feeds` — push data *out* to a peer Biggy
- `connections`: `id`, `name`, `base_url`, `active`. (The bearer `token` is a secret —
  re-enter it after import.)
- `feeds`: push rows of a local table to a peer. `id`, `name`, `active`,
  `source_table_id`, `connection_id`, `target_table` (remote table name), `mode`
  (`create`|`upsert`), `match_target_field`, `field_map` (JSON-string list of
  `{"target","source"}`; `source` is a local column or a `{token}` template),
  `event`+transition/condition keys (when to fire), `schedule_minutes`,
  `allow_manual`, `skip_api_writes`.

### `webhooks` — receive a push *in*
`id`, `name`, `active`, `target_table_id`, `mode` (`create`|`upsert`), `match_field`
(a column, or a comma-separated **composite** key like `"serial,site_id"`; matching is
case-insensitive and trimmed), `field_map` (JSON-string list of
`{"target": local col, "source": dotted JSON path}`),
`user_id`, abuse limits `max_body_bytes`/`rate_limit`/`rate_window`. A fresh receive
token is minted on import (rotate it in the UI to get the URL); the optional HMAC
`secret` is not imported.

### `pull_sources` — poll a remote source *in*
`id`, `name`, `active`, `target_table_id`, `kind` (`"peer"` via a `connection_id` +
`remote_table`, or `"rest"` via `url`), `records_path` (dotted path to the records
array), `config` (JSON-string of advanced options: auth/pagination/request
templating/filter/transforms), `field_map`, `mode`, `match_field` (column or comma-separated composite;
normalized matching), `cursor_field`
(incremental watermark), `page_size`, `user_id`. Request `headers`/`auth_secret` are
secrets (re-enter after import); the `watermark` resets.

### `dashboards`, `dashboard_widgets` — composable BI pages (shared)
- `dashboards`: `id`, `name`, `description`, `columns` (1–3), `position`. (Only
  **shared** dashboards are in the schema; personal ones are per-user state.)
- `dashboard_widgets`: `id`, `dashboard_id`, `title`, `kind` (`chart`|`number`|
  `list`|`text`), `table_id`, `query` (a report query-string, e.g.
  `"group=status&metric=count"`; for `number` it is the metric only), `chart_type`
  (`bar`|`line`|`pie`), `content` (markdown for `text`; an optional numeric target
  for `number`), `width` (1–2), `limit` (rows for `list`), `position`.

### `sequences` — auto-number counters
`field_id` (an `autonumber` field), `next` (the next number to assign).

---

## 7. Secrets & runtime state

The export format deliberately omits secrets and resets runtime counters so a file
is safe(r) to share and re-import. When you author a file, you may include these and
they will be applied, but be aware that an *exported* file will not contain them:

| Object | Stripped on export / reset on import |
|---|---|
| `connections` | `token` (re-enter) |
| `webhooks` | receive token (re-minted), HMAC `secret` |
| `pull_sources` | request `headers`, `auth_secret`, `watermark` (reset to start) |
| `feeds` | `watermark` (reset) |
| `sla_policies` / `approval_steps` | the *config* is exported; the per-record **clocks** and approval **requests/actions** are runtime (not exported) |
| `data_sources` | `password` **is** included (needed to recreate tables) — treat exports as sensitive |

---

## 8. Rules & gotchas

- **`phys_name` / `name` identifiers** must match `^[a-z][a-z0-9_]*$` (start with a
  lower-case letter; only lower-case letters, digits, underscores), be reasonably
  short, and **not** start with the reserved prefixes `app_` or `j_`. Avoid SQL
  reserved words (`order`, `group`, `user`, …) for table names to stay portable.
- **Primary keys**: by default every table gets an auto-increment integer `id` — you
  don't define it as a field. To use a natural key, set `tables[].pk_col` to a
  field's `phys_name` and make that field `nullable: false`.
- **Relations stay within one data source** (you can't relate a home table to an
  adopted external table in another database).
- **Adopted tables** (`managed: false`) are mapped, not created — Biggy never issues
  DDL against them, so their columns/relations must already exist.
- **`enum`/`tags`** require `enum_options` (a JSON-string list); a `workflow` enum's
  `initial_state` and transition states must be members of that list.

---

## Instructions for an LLM

When asked to "build a Biggy app", produce **one JSON file** following these rules:

1. Start with `{"version": 1, ...}`. Output **only** valid JSON (no comments, no
   trailing commas).
2. Plan ids first: give every `tables`, `fields`, `forms`, `relations`,
   `dashboards`, `dashboard_widgets`, `connections`, `feeds`, `webhooks`,
   `pull_sources` object a unique integer `id`. Reuse those ids for references
   (§2). Never reference an id you didn't define.
3. For each table add its `fields`; set `tables[].display_field_id` to the field
   that best labels a row (usually a name/title). Make required fields
   `"nullable": false`.
4. **Many-to-one link** → add a `data_type:"relation"` field (with
   `related_table_id`) **and** a matching `relations` entry (`kind:"m1"`,
   `from_field_id` = that field). **Many-to-many** → add **only** a `relations`
   entry (`kind:"mn"`). (§4)
5. **JSON-encode nested values as strings**: `enum_options`, `field_map`,
   `transitions`, `layout`, `config`, `field_ids`, `*_display_field_ids` (§3).
6. Add at least one `forms` per table the user edits, list its `form_fields`, and a
   `menus` group with `form`/`dashboard` entries so it's reachable.
7. Leave out sections you don't need (they default to empty). Set user-reference
   fields (`notify_user_id`, webhook/pull `user_id`) to `null`.
8. Use valid `data_type` values from §5 and valid identifiers from §8.
9. **ITSM (optional):** an `sla_policies` entry references its `table_id` plus field ids
   (`status_field_id`, and write-back `state_field_id`/`due_field_id`); an
   `approval_steps` entry references a `workflows[].id` (`workflow_id`) and an
   `approver_role` from `roles`. See the `netcmdb` example for a worked model.

**Self-check before returning**: every `*_id` reference resolves to a defined object;
every `enum`/`tags` field has `enum_options`; every `m1` relation has both a relation
field and a `relations` entry; all nested-JSON fields are strings; the document
parses as JSON. A complete, valid example to mirror is
[`schema-reference.example.json`](schema-reference.example.json).
