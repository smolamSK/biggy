# Biggy — Setup & Operations

For the person **installing and running** Biggy. Covers install, the database,
first run, configuration, the scheduler, email, security, and backups. To *build*
an app see the [Designer manual](designer-manual.md); to *use* it see the
[User manual](user-manual.md); to *extend the code* see the
[Developer guide](developer-guide.md).

---

## Requirements

- **Python 3.11+** (developed/tested on 3.14).
- **MariaDB or MySQL** for the main database. (Secondary *data sources* may also be
  SQLite or other SQLAlchemy-supported databases — see [§ Data sources](#additional-data-sources).)

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # then edit credentials + SECRET_KEY
```

## The database

### Option A — system MariaDB

```bash
sudo systemctl start mariadb
mariadb -u root -e "CREATE DATABASE biggy CHARACTER SET utf8mb4;
  CREATE USER 'biggy'@'localhost' IDENTIFIED BY 'biggy';
  GRANT ALL ON biggy.* TO 'biggy'@'localhost';"
```

### Option B — throwaway local instance (no root / sandbox-friendly)

```bash
mariadb-install-db --no-defaults --datadir=$PWD/.localdb/data \
  --auth-root-authentication-method=normal
/usr/sbin/mariadbd --no-defaults --datadir=$PWD/.localdb/data \
  --socket=$PWD/.localdb/mysql.sock --port=3307 &
mariadb --no-defaults -S $PWD/.localdb/mysql.sock -u root \
  -e "CREATE DATABASE biggy; CREATE USER 'biggy'@'127.0.0.1' IDENTIFIED BY 'biggy';
      GRANT ALL ON biggy.* TO 'biggy'@'127.0.0.1';"
# then set DB_PORT=3307 in .env
```

Biggy creates and migrates its own `app_*` metadata tables automatically on
startup; you only provide an empty database and credentials.

## First run

```bash
.venv/bin/flask --app run init-db      # optional — the setup wizard does it too
.venv/bin/flask --app run run          # or: .venv/bin/python run.py
```

Open <http://127.0.0.1:5000>. On first run a **setup wizard** tests the connection
and creates the first **designer** account. (You can also create one from the CLI:
`flask --app run create-designer <username>`.)

---

## Configuration (`.env`)

All settings are environment variables, loaded from `.env` at startup (see
`app/config.py`). Connection parts and a few common knobs:

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | dev value | **Set this in production** — Flask session signing key |
| `DB_DRIVER` | `mysql+pymysql` | SQLAlchemy driver |
| `DB_HOST` / `DB_PORT` / `DB_USER` / `DB_PASSWORD` / `DB_NAME` | `127.0.0.1` / `3306` / `biggy` / `biggy` / `biggy` | connection parts |
| `DATABASE_URL` | — | full SQLAlchemy URL; **overrides** the `DB_*` parts |
| `UPLOAD_FOLDER` | `<instance>/uploads` | where file/image uploads are stored |
| `MAX_CONTENT_LENGTH` | `16777216` (16 MiB) | max upload/request size |
| `CURRENCY_SYMBOL` | `$` | symbol for `currency` fields |

### Email (trigger emails + scheduled report digests)

Email actions and scheduled report digests are **skipped** unless `MAIL_SERVER` is
set (and always skipped under tests).

| Variable | Default | Meaning |
|---|---|---|
| `MAIL_SERVER` | — | SMTP host (unset = email disabled) |
| `MAIL_PORT` | `25` | SMTP port |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | — | SMTP auth (optional) |
| `MAIL_USE_TLS` | `false` | STARTTLS |
| `MAIL_DEFAULT_SENDER` | `biggy@localhost` | From address |
| `NOTIFY_WEBHOOK_TIMEOUT` | `5` | timeout (s) for outbound email/webhook/connector calls |

### Inbound-webhook limits (defaults; each webhook may override in the UI)

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_MAX_BODY_BYTES` | `65536` (64 KiB) | reject larger bodies (413) |
| `WEBHOOK_RATE_LIMIT` | `120` | requests per window per webhook (`0` = off) |
| `WEBHOOK_RATE_WINDOW` | `60` | window seconds |

### Scheduler

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULER_ENABLED` | `false` | start the in-process job ticker (see [§ Scheduler](#the-scheduler)) |
| `SCHEDULER_TICK_SECONDS` | `60` | ticker interval |

---

## Running

### Development

```bash
.venv/bin/flask --app run run     # http://127.0.0.1:5000, debug on
```

### Production

`run.py`'s `app.run(debug=True)` is for development only. In production:

- Serve with a real WSGI server, e.g. `gunicorn 'app:create_app()'` (or uWSGI),
  behind a TLS-terminating reverse proxy (nginx/Caddy).
- Set a strong `SECRET_KEY` and real DB credentials; never run with `debug=True`.
- Put a **request-body size limit** at the proxy in front of the public, unauthenticated
  `POST /hooks/<token>` webhook endpoints (defence in depth on top of
  `WEBHOOK_MAX_BODY_BYTES`).
- Cookies are already `HttpOnly` + `SameSite=Lax`; serve over HTTPS so they're secure.

---

## The scheduler

Time-driven work — **scheduled triggers** (reminders/escalations), **scheduled
feeds**, and **scheduled report email digests** — is run by one entry point. Pick
**one** of:

- **External cron (recommended at scale):** run the CLI on an interval:
  ```cron
  * * * * *  cd /path/to/biggy && .venv/bin/flask --app run run-jobs
  ```
  (`flask --app run sync` is a kept alias of `run-jobs`.)
- **In-process ticker (single-process deploys):** set `SCHEDULER_ENABLED=true` and a
  `SCHEDULER_TICK_SECONDS`; a background thread runs due jobs. **Caveat:** it is
  per-process, so under multiple workers each would tick — use the cron form (one
  runner) when you scale out.

Manage and "Run now" jobs in the UI at **Designer → Admin → Scheduled jobs**.

---

## Integration security

- **Inbound webhooks** authenticate by a secret token in the URL (stored hashed,
  shown once); add an HMAC `secret` to require signed bodies; per-webhook size +
  rate limits apply (defaults above).
- **Pull sources** store request `headers` / `auth_secret` as secrets.
- **REST API** uses per-user bearer tokens (revocable; stored hashed). The auto docs
  live at `/api/v1/docs`.
- **Outbound** connectors/feeds and trigger webhooks call out over HTTP — restrict
  egress as appropriate.

---

## Backup, restore & upgrades

- **Backup** (*Designer → Admin → Backup*): export the **schema** (the whole model)
  and the **data** as JSON; import to copy an app between databases. You can also
  author a model by hand — see [Schema JSON format](schema-json-format.md).
- **Sensitive:** schema export **includes `data_sources` passwords** (needed to
  recreate tables in those databases). Connection tokens, webhook tokens/secrets and
  pull-source secrets are **redacted** and re-entered after import.
- **Upgrades:** on startup Biggy runs `create_all` + `ensure_meta_schema`, which
  adds any new `app_*` columns idempotently — so pulling a newer version and
  restarting is the upgrade path. No manual migration step.

---

## CLI reference

```bash
flask --app run init-db                  # create the app_* metadata tables
flask --app run create-designer <user>   # create a designer account (prompts for password)
flask --app run run-jobs                 # run all due scheduled jobs once (cron target)
flask --app run sync                     # alias of run-jobs
flask --app run dump-examples [dir]      # write the bundled example schemas/data to a dir
```

---

## Running the tests

Unit tests need no database; integration tests use a dedicated `biggy_test`
database and are skipped if it is unavailable. Create it once:

```sql
CREATE DATABASE biggy_test; GRANT ALL ON biggy_test.* TO 'biggy'@'127.0.0.1';
-- (a second `biggy_test2` enables the multi-data-source tests)
```

```bash
.venv/bin/python -m pytest
```

See the [Developer guide](developer-guide.md) for architecture and how to extend.
