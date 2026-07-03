# Changelog

All notable changes to Biggy are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/); versions follow
[Semantic Versioning](https://semver.org/).

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
