# Biggy — Designer Manual

A quickstart for **Designer mode**: building the data model, forms, and menus that
power **User mode**. Everything you define is stored as metadata **and** turned
into real database tables, so your data lives in genuine, query-able tables with
real foreign keys.

## The two modes

The top bar links switch between **Designer** (build) and **User mode** (use what
you built). Designer mode is available to accounts with the **designer** role;
manage accounts under **Users**. The left sidebar groups the designer tools:
**Model**, **Interface**, **Data**, **Integrations**, **Admin**, and your
**Tables**.

## Quickstart: from nothing to a working screen

Four steps take you from an empty app to a usable form:

1. **Create a table** — *Tables → ＋ New table*. Give it an identifier
   (`lowercase_with_underscores`) and a label, then **add fields**. By default a
   table gets an auto-increment `id` key; use the **primary-key chooser** if you
   need a natural key (e.g. a `code` column you enter yourself) instead.
2. **Add fields** — choose a type and options for each column (see below).
3. **Build a form** — *Interface → Forms → New*, bind it to your table, and add
   the fields to show.
4. **Add it to a menu** — *Interface → Menus*, add your form (or a table's list
   view) so it appears in User mode.

Switch to **User mode** and your screen is live.

Two shortcuts collapse most of that:

- **Generate form** (on any table's page) creates the data form with every field
  and relation already placed — optionally a read-only view form and a menu entry
  too, so steps 3–4 become one click.
- **＋ From CSV** (in the Tables menu) bootstraps from a spreadsheet export: column
  names and types are inferred from the file, you review them, and the table is
  created with all rows imported.

## Tables & fields

Open a table to add, edit, reorder (▲▼), or drop fields. Each field has a **type**:

- **Text (short / long)**, **Integer / Big integer**, **Decimal / Float**,
  **Boolean**, **Date / Date & time / Time**.
- **Choice list (enum)** and **Tags (multi-select)** — supply the options, one per
  line. When editing an enum field you can also pick a **status color** per option
  (the chips shown in lists and record pages); “auto” derives a stable color.
- **User** — references an app account (an *assignee*): rendered as a username
  picker, shown as the username, filterable by **Me** (so an "assigned to me"
  saved view works for everyone), with an **Assign to me** button on record pages
  and a **My work** panel on the User-mode home. Default value `me` assigns the
  creator automatically.
- **Company** — references a company from the **company tree** (*Admin →
  Companies*) and turns on **data separation** for the table: users assigned a
  company (Users page) see only rows of their company *and the companies below
  it*; rows they create are stamped with their company automatically. Users
  without a company, and designers, see everything. Applies to lists, search,
  reports, dashboards, the API and the impact map.
- **Email**, **URL**, **Phone** — validated and rendered as clickable links.
- **Currency**, **Percent** — numeric with formatted display.
- **JSON** — stores/validates a JSON value.
- **Auto-number** — a generated sequence (e.g. `INV-0001`); read-only on forms.
- **Formula** — a value computed from this row (and **related tables** via
  `lookup()` / `rollup()`); recalculated automatically on save. Read-only.
- **Markdown** — long rich text written as markdown, rendered as formatted HTML on
  record pages (raw HTML is neutralized). Great for knowledge-base articles.
- **Image / File** — uploads, shown as a thumbnail or download link.

Per-field options include **length / precision / scale**, **nullable**,
**unique**, a **default value**, and **validation rules** (min/max length,
number range, regex pattern). Defaults can use tokens — `now`, `today`,
`current_user` — filled in automatically when a record is created.

Other table-level controls:

- **Display field** — the column used to label this table's records in pickers.
- **Behaviour flags** — turn on **audit** (change history), **soft delete**
  (Trash + restore), and **row ownership** (users see only their own rows).
- **Unique constraints** — add a **composite** (multi-column) unique rule.
- **Duplicate table** — copy the structure (fields, options, uniques) under a new
  name; data and relations are not copied.

## Relations

*Model → Relations*:

- **Many-to-one** — adds a foreign-key column on the "from" table (e.g. an order's
  *customer*). Set the on-delete behaviour.
- **Many-to-many** — creates a junction table linking two tables.

For each relation you can choose which fields **label** the related record in
pickers. See the whole model visually under *Model → Diagram* (an ER diagram). At the
data level, users can open an **Impact map** of any record from its view page — a
node-link graph of what it depends on and what depends on it, built from these
relations (no setup needed).

## Forms

*Interface → Forms*. A form is bound to one table and has a **purpose**:

- **Data entry** — the add/edit form used in User mode.
- **View** — the read-only record page.

A data-entry form can also join the **service catalog**: it then appears as a
request card on the User-mode *Catalog* page, and submissions show up under each
user's *My requests*. Manage the whole catalog at once under *Interface →
**Catalog*** (per-form checkbox + group + card description; the same controls also
sit on each form-edit page). The *Catalog* and *My requests* menu links appear in
User mode only once something is published. Enable **row ownership** or **audit**
on the table so submissions are owner-stamped — the Catalog page warns you when
they aren't.

Add **items** to a form: **fields**, **many-to-many** pickers, and **section
headings** to group the layout. Per item you can set a label override, help text,
**required**, **read-only**, and — for relation items — a **dependent picker**
(filter this drop-down by the value of another field). Reorder items with ▲▼.
**Add all missing fields** places every remaining field/relation in one click, and
**Duplicate** copies the whole form. **List defaults** (bottom of the form editor)
set how the form's list opens — sort column, direction, page size; a visitor's own
sorting and saved views still win.

## Menus

*Interface → Menus*. Build the User-mode sidebar from **groups** (headings) and
links to a **form** or a **list view** of a table. Order them, nest them under
groups, and pick an optional **icon** — it shows in the sidebar and on the home
page's quick-access cards.

## Recurring records

*Admin → Recurring*: create a templated record on a cadence (hourly / daily /
weekly / monthly / custom minutes) — preventive-maintenance tickets, recurring
audits, renewal reminders. Values are `column = value` lines validated like CSV
imports; unlisted columns use their field defaults. Runs with the scheduler and
is claimed atomically, so multiple workers never double-create. Pause/resume per
job.

## Maintenance windows

*Admin → Maintenance*: schedule planned-work periods, scoped to one table or all.
While a window is **active**, SLA breaches and escalations are **held** (they fire
after the window ends if still overdue) and trigger/watch notifications are
suppressed — trigger *data* actions (set field, create record) still run, and
record conversations are never muted. Lists and record pages show a banner while
a window is active. Optionally **link a window to the change record** that
motivates it (or use *Plan maintenance* on the record itself) — the window then
appears on that record's page.

## Customer portal

The third mode, next to Designer and User: a narrow surface at `/portal` for
**external customers**.

1. Create accounts with the **portal** role (*Users* page or bulk import —
   `username,portal[,password[,company]]`). Portal users are locked out of
   Designer and User mode; signing in lands them on the portal.
   Assign colleagues the same **Company** and they see each other's tickets
   (list, ticket page, comments, closing); a portal user of a **parent** company
   also sees the tickets of every company below it in the tree. Accounts without
   a company stay strictly personal. Staff accounts never widen a portal scope.
2. Publish request/incident forms via the **catalog** (above). A form being in
   the catalog *is* what grants portal access to it; only tables with **audit**
   or **row ownership** work there (everything is scoped to the record creator —
   the Catalog page warns when the stamps are missing).
3. Customers submit requests, see **only their own tickets** (status chip, dates,
   read-only field summary from the catalog form's items), and communicate
   through the record **conversation**. They cannot edit fields — staff works
   the ticket in User mode and replies with **Reply to customer**; **internal
   notes** never appear in the portal. Public replies notify the customer
   (portal bell), and customer comments notify the staff participants.
4. Optionally let customers **close their own tickets**: pick the closing status
   in the Catalog page's *Customers may close* column. The close button applies
   the status through the normal write path (audit, triggers, SLA stop) and posts
   a public "Closed by customer" comment; with a workflow, the current → close
   transition must exist (approval-gated transitions never offer the button).

## Instance settings (branding)

*Admin → Settings*: rename the application (top bar + sign-in page), pick an
**accent color** applied across all themes, and choose the **default theme**
visitors get before they pick their own. Stored in the database; blank fields
fall back to the server configuration.

## Existing databases & multiple sources

- **Data sources** (*Data → Data sources*) — register another database (MariaDB,
  SQLite, …). New tables can be created in it, and its existing tables can be
  mapped (below).
- **Merge records** (*Model → Merge records*) — reconcile duplicates: pick a survivor
  and a duplicate; references are repointed, links move over, the survivor's empty
  fields are filled, and the duplicate is deleted. Upserts from webhooks/pulls/CSV can
  also match on **composite keys** (comma-separated columns, case-insensitive).
- **Adopt tables** (*Model → Adopt tables*) — map a table that already exists in a
  database (yours or a registered source) into Biggy without recreating it. Adopted
  tables are **read-only structurally** (Biggy never alters them) but get forms,
  views, and workflows like any other.

## Going further

These features are optional — add them as your app grows.

- **Workflows** (*Model → Workflows*) — attach a status graph to an enum field:
  allowed transitions (optionally limited by role), an initial state, and a
  visual editor. User-mode edits and the API are held to the graph.
- **Triggers & notifications** (*Admin → Triggers*) — when a record is created,
  updated, transitions, is deleted, or on a **schedule** (with an optional
  condition), run actions: an in-app notification, an email, a webhook (full JSON or
  a `{"text": …}` message for **Slack / Microsoft Teams** incoming webhooks), **set a
  field**, or **create a record** in another table (templated field map; chained
  creation is depth-capped). Messages use `{field}` placeholders. *Scheduled* triggers run over every
  matching row — pair a condition with a *set-field* so a row isn't actioned twice.
- **SLA policies** (*Admin → SLA policies*) — put a service-level target on a table.
  A per-record clock starts/pauses/stops from a **status field** (you list the running
  / paused / done states) and measures 24×7 time against the target; the live state and
  deadline are **written back to fields you pick** (so they show in lists/reports and
  can drive triggers). The scheduler detects **breaches** and escalates (in-app / email
  / set a field), then walks an optional **escalation chain** — JSON levels fired as
  the breach ages (e.g. notify the owner after 30 min, email the NOC after 60). A live
  SLA panel appears on the record's view page.
- **Approvals** (*Admin → Approvals*) — require multi-step **sign-off on a workflow
  transition**. Add approver **steps** (a role or a specific user) to a transition;
  same *position* runs in parallel (all must approve), different positions run in
  sequence. Requesting that transition then **holds** the record until everyone signs
  off (or one rejects). Pairs with **Workflows**; approvers act from the record's
  Approvals panel or the approvals inbox.
- **Scheduled jobs** (*Admin → Scheduled jobs*) — one view of every time-driven job
  (scheduled triggers, feeds, report digests, SLA breach sweeps) with last-run status
  and **Run now**. They run from `flask run-jobs` (cron) or the in-process ticker — see
  the [Setup & operations](setup-and-operations.md) guide.
- **Reports** (*Data → Reports*) — group-by + count/sum/avg with an optional chart;
  **save**, **pin** to home, **email on a schedule**, or **add to a dashboard**.
- **Dashboards** (*Interface → Dashboards*) — build shared pages of **chart**, **KPI
  number**, **list**, and **text/markdown** tiles (set the column count). Link one
  into the nav with a **dashboard** menu item. (Users also build personal dashboards
  in User mode.)
- **Access control** (*Admin → Roles*, *Permissions*) — define roles, set
  per-form access (none / read / write), and set **per-field** permissions. Manage
  accounts under **Users** (top bar): create/edit users, **bulk import** (paste
  `username,role[,password]` lines), and **reset a user's two-factor**. Two-factor
  (2FA) and single sign-on (SSO) are set up in the
  [Setup & operations](setup-and-operations.md) guide.
- **Integrations** (*Integrations*):
  - **Connections + Feeds** — chain Biggy apps: define a **connection** to a remote
    app (base URL + API token), then a **feed** mapping a local table to a remote
    table, pushing on an event / schedule / on demand (create or upsert; can drive
    the remote workflow).
  - **Webhooks** — receive events *in*: each webhook gives a secret URL that turns a
    posted JSON body into a record (create/upsert), with an optional HMAC signature
    and per-webhook size/rate limits.
  - **Pull sources** — poll *in*: fetch from a Biggy peer or any REST API on a
    schedule and upsert locally, with cursor (incremental) pulls and customizable
    auth, pagination, field-mapping and transforms.
- **REST API** — every table is at `/api/v1/<table>` (list/get/create/update/delete
  + **bulk**), authenticated by per-user **API tokens**. A self-describing
  **OpenAPI** spec + docs live at `/api/v1/docs`.
- **Backup** (*Admin → Backup*) — export/import the **schema** (the whole model)
  and the **data** as JSON, to copy an app between databases. You can also author a
  model by hand — see **[Schema JSON format](schema-json-format.md)** for the full
  format and a complete template.
- **Examples** (*Admin → Examples*) — load a ready-made demo model to explore.
- **SQL console** (*Data → SQL*) — run read-only queries against your data.
- **Audit log** (*Admin → Audit log*) — review changes on audited tables.
- **Connection** (top bar) — see the current database target and a live
  connection test. Change connection settings in `.env` and restart the app.

---

Looking to *use* the screens you built? See the **User manual**. Deploying or
extending Biggy? See the [Setup & operations](setup-and-operations.md) and
[Developer](developer-guide.md) guides.
