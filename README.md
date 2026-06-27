# Biggy

A metadata-driven, low-code app for managing data in a relational database. It has two modes:

- **Designer mode** — create tables, define fields and relations (many-to-one and many-to-many),
  and design the menus and data-entry forms used in User mode.
- **User mode** — use the generated forms to add records, search, and edit / delete / clone data.

Designer mode writes definitions into metadata tables (`app_meta_*`) **and** issues real DDL, so
your user data lives in genuine, query-able MariaDB tables with real foreign keys. The database
connection is configurable; the default target is a local MariaDB.

## Stack

Flask + Jinja + HTMX (server-rendered, no JS build step), SQLAlchemy 2 (Core for dynamic DDL +
reflection, ORM for the metadata), PyMySQL driver, Flask-Login (accounts + `designer`/`user` roles,
TOTP 2FA, OIDC SSO), Flask-WTF/WTForms, `cryptography` (secrets at rest), `qrcode` (MFA enrollment).

## Requirements

- Python 3.11+ (developed/tested on 3.14)
- MariaDB (or MySQL) server

## Setup

> Quick-start below. For the full deployment/ops reference — every env var,
> production run, the scheduler, email, security and backups — see
> **[docs/setup-and-operations.md](docs/setup-and-operations.md)**.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit credentials / SECRET_KEY
```

### Start MariaDB and create the database

Using the system service (needs privileges):

```bash
sudo systemctl start mariadb
mariadb -u root -e "CREATE DATABASE biggy CHARACTER SET utf8mb4;
  CREATE USER 'biggy'@'localhost' IDENTIFIED BY 'biggy';
  GRANT ALL ON biggy.* TO 'biggy'@'localhost';"
```

Or run a throwaway local instance with no root access (handy for sandboxes):

```bash
mariadb-install-db --no-defaults --datadir=$PWD/.localdb/data --auth-root-authentication-method=normal
/usr/sbin/mariadbd --no-defaults --datadir=$PWD/.localdb/data \
  --socket=$PWD/.localdb/mysql.sock --port=3307 &
mariadb --no-defaults -S $PWD/.localdb/mysql.sock -u root \
  -e "CREATE DATABASE biggy; CREATE USER 'biggy'@'127.0.0.1' IDENTIFIED BY 'biggy';
      GRANT ALL ON biggy.* TO 'biggy'@'127.0.0.1';"
# then set DB_PORT=3307 in .env
```

### Run

```bash
.venv/bin/flask --app run init-db      # create the app_* metadata tables (optional; setup does it too)
.venv/bin/flask --app run run          # or: .venv/bin/python run.py
```

Open http://127.0.0.1:5000 — on first run you'll get a **setup wizard** that tests the connection
and creates the first designer account.

## Connection configuration

Set in `.env` (loaded at startup):

| Variable | Meaning |
|---|---|
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | connection parts |
| `DB_DRIVER` | SQLAlchemy driver, default `mysql+pymysql` |
| `DATABASE_URL` | full SQLAlchemy URL; overrides the parts above |
| `SECRET_KEY` | Flask session signing key |

The current target (with the password masked) and a live test are shown on the **Connection** page
in Designer mode. Change connection settings in `.env` and restart the app.

## Typical workflow

1. **Designer → New table**, then add fields (text, number, decimal, boolean, date/time, choice list).
2. **Relations**: add many-to-one (a foreign-key column) or many-to-many (an auto-created junction table).
3. **Forms**: create a form bound to a table and add the fields/relations to show.
4. **Menus**: add the form (or a table list view) to the User-mode navigation.
5. **User mode**: enter and manage data with search, edit, clone and delete.

## Documentation

Full guides live in [`docs/`](docs/README.md), one per audience:

- **Use it** — [User manual](docs/user-manual.md) · [Designer manual](docs/designer-manual.md)
- **Set it up** — [Setup & operations](docs/setup-and-operations.md) (install,
  config, scheduler, security, backups, CLI)
- **Implement / extend it** — [Developer guide](docs/developer-guide.md) (architecture,
  module map, recipes)
- **Reference** — [Schema JSON format](docs/schema-json-format.md) (author an app as
  a single import file; humans + LLMs)

The user, designer, setup, and developer guides are also available **inside the app**
via the **Help** link in the top bar.

## Tests

```bash
.venv/bin/python -m pytest
```

Unit tests (identifier validation, type mapping, DDL generation) need no database. Integration tests
use a dedicated `biggy_test` database and are skipped automatically if it is unavailable. Create it with:

```sql
CREATE DATABASE biggy_test; GRANT ALL ON biggy_test.* TO 'biggy'@'127.0.0.1';
```

## Security notes

- Table/column identifiers are validated on creation (`^[a-z][a-z0-9_]*$`, length-capped, reserved
  prefixes blocked) and thereafter only ever sourced from metadata and emitted through SQLAlchemy
  objects — never string-interpolated from request input. All data **values** are bound parameters.
- CSRF protection on all mutating forms; passwords hashed; Designer mode and user management guarded
  by a `designer` role.
- Optional **two-factor (TOTP)** and **OIDC single sign-on**; integration secrets and TOTP seeds are
  **encrypted at rest** (Fernet). See [Setup & operations](docs/setup-and-operations.md) for setup.

## Layout

```
app/
  config.py db.py identifiers.py helpers.py    # config, engine/session registry, identifier safety
  metadata/ models.py schema_service.py ddl.py field_types.py  # ORM metadata + portable DDL
  record_service.py data_service.py formula.py # write chokepoint; CRUD/search; formulas
  forms/ builder.py admin_forms.py             # dynamic + fixed forms
  workflow.py triggers.py reporting.py dashboards.py  # workflows; rules; reports + charts
  approvals.py sla.py topology.py jobs.py      # approval workflows; SLA engine; impact map; atomic job claim
  crypto.py oidc.py totp.py                    # secrets at rest; OIDC SSO; TOTP two-factor
  connectors.py feeds.py hooks/ pull.py scheduler.py  # integrations: push out / in / poll / schedule
  api/ routes.py serialization.py tokens.py openapi.py  # REST API + OpenAPI + bulk
  schema_io.py adopt.py                        # JSON schema/data import-export; adopt external tables
  core/ auth/ designer/ user/ (routes.py)      # blueprints
  templates/ static/                           # Jinja templates, CSS, HTMX, hand-rolled SVG charts
tests/                                         # unit + integration tests (biggy_test)
run.py requirements.txt .env.example
```

See **[docs/developer-guide.md](docs/developer-guide.md)** for the architecture.

## Beyond the basics

The model and screens extend well past the core CRUD loop. Built in:

- A rich field-type set (email/URL/phone, currency/percent, JSON, multi-select tags,
  auto-number, **formula**, file/image uploads), default expressions, validation
  rules, and composite-unique constraints; **arbitrary primary keys**.
- **Multiple data sources** and **adopting pre-existing tables** in other databases
  (MariaDB/MySQL/SQLite…), mapped without recreating them.
- Status **workflows**, **triggers & notifications**, and a **scheduler** (run
  triggers/feeds/report-digests on a cadence).
- **Reports & dashboards** — group-by + charts, shared & personal **dashboards**
  (chart/KPI/list/text tiles).
- Access control: custom **roles**, per-form and **field-level** permissions; audit
  history, soft-delete/Trash, and row ownership.
- A token-authenticated **REST API** (`/api/v1`) with an auto **OpenAPI** spec +
  docs and **bulk** endpoints; **chaining** between instances (connections + feeds),
  inbound **webhooks**, and **pull** connectors (poll a peer or REST API).
- **CMDB / ITSM**: a data-level **impact map** (a record's dependency/impact graph), an
  **SLA engine** (per-record clocks with pause/resume, breach detection + escalation),
  and **approval workflows** (multi-step sign-off held on a workflow transition).
- **Enterprise auth & ops**: **TOTP two-factor** (QR enrollment + backup codes), **OIDC
  single sign-on** (link-existing or JIT), **bulk user import**, integration **secrets
  encrypted at rest**, multi-worker-safe scheduling + a DB-backed rate limiter, and a
  **Docker**/compose stack.
- Schema/data **export & import** (JSON) to copy an app between databases — and to
  [author a whole app by hand](docs/schema-json-format.md).
