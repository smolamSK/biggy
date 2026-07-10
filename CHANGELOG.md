# Changelog

All notable changes to Biggy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

## [2.0.0] — 2026-07-10

Biggy grows from a low-code database tool into a full **CMDB / ITSM platform**
with three modes (Designer, User, customer **Portal**) and multi-tenant data
separation. Upgrades from 1.x are seamless: new metadata columns and tables
backfill automatically on boot; nothing existing is changed.

### Added — customer portal (third mode)
- **Portal mode** (`/portal`) for external `portal`-role accounts: submit
  requests/incidents from the service catalog, track own tickets (status chips,
  read-only field summary defined by the catalog form), attachments, a
  notifications page. Portal users are isolated from Designer and User mode.
- **Record conversations**: a per-record thread with a ServiceNow-style split —
  **Reply to customer** vs **internal note** (never shown in the portal) — on
  every record page; participants are notified in-app and by email.
- **Close my ticket** (designer opt-in per catalog form): customers close their
  own tickets into a designer-chosen status; workflow edges and approval gates
  are honored, and a public "Closed by customer" comment notifies staff.
- **Company-scoped sharing**: portal colleagues of the same company see each
  other's tickets; a parent-company account sees the whole chain below.

### Added — companies & multi-tenancy
- **Company tree** (*Admin → Companies*): companies chain via a parent — access
  to one implies access to everything below it. Users (staff and portal) are
  assigned a company on the Users page / bulk import.
- **`company` field type**: adding it to a table turns on per-tenant data
  separation, enforced in the read chokepoint — lists, search, reports,
  dashboards, kanban, the REST API and the impact map all inherit it. Records
  created by scoped users are auto-stamped; relation pickers and filter
  dropdowns only offer the chooser's subtree (no tenant name leaks).

### Added — ITIL process modules
- **Enable-able processes** — Incident management (priorities, workflow, 4h
  resolution SLA, portal card), Request fulfilment, Problem management + a
  known-error database, Change management (type/risk, implementation & backout
  plans, CAB approval) — switched on at setup or any time later, added
  *additively* next to the existing model (new `import_schema(additive=True)`).
- Modules **wire themselves together** in any order: incident → problem,
  change → problem, incident → change, and incident/request/change → `ci`
  links appear automatically once both tables exist; module tables carry the
  tenant Company field (known errors stay global).
- A complete **ITSM / service desk example**: all four processes wired to a
  small CMDB (services + CIs) with cross-linked sample data.

### Added — staff (NOC) toolkit
- **Assignments**: a `user` (assignee) field type, a **"Me" filter token** so
  one shared saved view works for everyone, an **Assign to me** button, a
  **My work** home panel, and "assigned to you" notifications.
- **SLA where triage happens**: a color-coded time-to-breach column on lists
  and an "SLA — due next" home panel.
- **Watch a record**: subscribers get notified of every update (any write
  path) and comment; **Activity stream** (`/u/activity`): all changes +
  comments across readable tables, filterable — built for shift handover.
- **Bulk edit**: set a field across selected rows through the write chokepoint,
  with per-row workflow/approval/scope guards.
- **Maintenance windows** (linkable to change records): SLA breaches and
  escalations held, trigger/watch alerts suppressed, banners shown while
  active; **recurring records** create templated tickets on a cadence via the
  multi-worker-safe scheduler.
- **Email for people**: accounts have an email + opt-out; conversation
  replies, watch updates, assignments and portal notifications go out as real
  mail with links (Settings-page base URL).

### Added — designer productivity & customization
- **Generate form from table** (one click, optional view form + menu entry),
  **table-from-CSV wizard** (types sniffed, rows imported), **duplicate**
  table/form, a central **service-catalog editor**, **Settings** (app name,
  accent color, default theme, base URL), rendered **menu icons**, per-option
  **enum chip colors**, per-form **list defaults** (sort/page size).

### Added — interface modernization
- Inline SVG icon set (no more OS-dependent emoji), self-hosted Inter,
  **Ctrl+K command palette**, account dropdown, row action menus + toolbar
  overflow, removable filter chips, a real home page, searchable relation
  pickers, breadcrumbs, live badges, unsaved-changes guard, off-canvas mobile
  navigation, accessibility pass (skip link, ARIA tabs, `aria-sort`), chart
  tooltips, friendly empty states.

### Changed
- The app now has **three modes**; the user-mode Catalog / My requests links
  appear only once the catalog has content.
- The topbar was decluttered into an account menu; brand name and accent
  follow the Settings page.

### Removed
- The short-lived `AppUser.organization` text label (never in a release) was
  replaced by the Company tree; the leftover column on interim databases is
  harmless.

## [1.1.0] — 2026-07-03

### Added
- **Global full-text search**: the *Search all…* page matches every text field of
  every viewable table, with the matched field named and the term highlighted, plus
  "view all" links into filtered lists; the per-list search box now also matches
  across all of a table's text columns.
- **Reconciliation**: upserts (webhooks, pull sources, CSV import) match on
  **composite keys** (comma-separated columns) with normalized (case-insensitive,
  trimmed) comparison; a **Merge records** designer tool folds a duplicate into a
  survivor — references repointed, links moved, blanks filled, audit-logged.
- **SLA escalation chains**: JSON levels fired in order as a breach ages (notify the
  owner/a user, email), tracked per clock.
- **Create-record trigger action**: a rule can create a templated record in another
  table (chained creation depth-capped); **Slack / Microsoft Teams** delivery via a
  `{"text": …}` webhook payload option.
- **Service catalog & self-service portal**: flag any form as a catalog card
  (grouped) on `/u/catalog`; users track their submissions under **My requests**.
- **Markdown field type** (rendered safely — raw HTML neutralized) and a
  **Knowledge base** example (categorized markdown articles with a
  draft/published workflow), searchable via global search.

### Changed
- OpenAPI info version now follows the app version.

## [1.0.0] — 2026-07-03

First tagged release. Biggy is a metadata-driven, low-code platform for building
relational-database apps: Designer mode writes definitions into `app_meta_*` tables
**and** issues real DDL, so user data lives in genuine tables with real foreign keys.

### Core platform
- Designer mode: tables, 24 field types (incl. formulas with `lookup()`/`rollup()`,
  auto-number, files/images), m1/mn relations, forms (data + read-only views), menus,
  validation, composite uniques, arbitrary/natural primary keys, ER diagram.
- User mode: generated lists (search/filter/sort/columns/saved views), record pages
  with related-record tabs + change history, inline edit, clone, kanban, calendar,
  bulk actions, CSV import/export, trash/restore, personal + shared dashboards,
  reports (group-by + charts, scheduled email digests), notifications, global search.
- Multiple data sources (MariaDB/MySQL/SQLite) and **adoption** of pre-existing
  tables; portable DDL via Alembic operations.
- Schema + data **export/import as JSON**, with a documented authoring format for
  humans and LLMs (`docs/schema-json-format.md`) and built-in example apps.

### CMDB / ITSM
- **Impact map**: a data-level dependency/topology graph for any record.
- **SLA engine**: per-record clocks driven by a status field (pause/resume), live
  state written back to record fields, breach detection + escalation via the scheduler.
- **Approval workflows**: multi-step (sequential/parallel) sign-off held on a workflow
  transition, with an approvals inbox and a per-record decision trail.
- The large *Network CMDB* example ships with an incident SLA and change-request
  approvals defined in its schema JSON.

### Integrations & API
- REST API (`/api/v1`) with per-user tokens, auto-generated OpenAPI docs and bulk
  endpoints; instance chaining (connections + feeds), inbound webhooks (HMAC,
  size/rate limits), and pull connectors (cursor/pagination/auth/transforms).
- General scheduler (cron `run-jobs` or in-process ticker) for triggers, feeds,
  pulls, report digests and SLA sweeps.

### Security & operations
- TOTP two-factor authentication (QR enrollment, backup codes, admin reset,
  optional `REQUIRE_MFA`), OIDC single sign-on (link-existing or JIT), bulk user
  import, login/MFA failure lockouts, hardened sessions.
- Integration secrets and TOTP seeds encrypted at rest (Fernet).
- Multi-worker-safe scheduling (atomic DB job claims) and a DB-backed shared rate
  limiter; Dockerfile + docker-compose stack.
- CI (GitHub Actions): ruff lint + the full 209-test suite against MariaDB.
