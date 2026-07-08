# Biggy — User Manual

A quickstart for **User mode**: entering, finding, and managing data using the
forms and menus a designer has set up for you. (If you also build the data model,
see the **Designer manual**.)

## Signing in

Open the app and sign in with the username and password you were given. After
signing in you land on **Home**. If single sign-on is enabled, use the **Sign in
with SSO** button to sign in with your organization account instead.

If you ever need to change your password, open the **account** link (your
username, top-right) → *Change password*.

### Two-factor authentication (2FA)

Protect your account with a one-time code from an authenticator app
(Google Authenticator, Authy, 1Password, …):

1. Open your **account** → *Two-factor*.
2. **Scan the QR code** (or paste the key) into your authenticator app.
3. Enter the 6-digit code to confirm. **Save the backup codes** shown — each works
   once if you lose your device.

After that, signing in asks for the code as a second step. To turn it off, return to
the same page and enter a current code. If you lose your device, a designer can
**reset** your two-factor from the Users page.

## The screen at a glance

- **Top bar** — the Biggy brand, a **theme** picker (Light / Dark / Sepia / Ocean
  / High contrast), the **🔔 notifications** bell, **Help**, your account, and
  **Sign out**.
- **Left menu** — the navigation a designer built: groups you can expand/collapse,
  and links that open a **list** of records. A **Search all…** box at the top
  searches **every text field of every table you can see**; results are grouped by
  table and show which field matched, with the term highlighted.
- **Main area** — whatever you opened (a list, a record, a report…).

## Opening a list

Click a menu entry to open a **list** of records (for example *Customers*). From
a list you can:

- **Search** — type in the search box to match text across all the table's text
  columns (names, notes, emails, statuses, tags, …).
- **Filter** — click **+ Add condition** to filter by a field (equals, contains,
  greater than, is empty, …), then **Apply**.
- **Sort** — click a column header to sort; click again to reverse.
- **Columns** — use **Columns ▾** to show/hide columns.
- **Paginate** — page through results and change the page size.

### Saved views

Once you have a search/filter/sort you like, click **Save view** to name it.
Saved views appear above the list so you can reapply them in one click.

## Adding and editing records

- **Add** — click **New** (or **Add**) above the list and fill in the form.
  Required fields are marked; the form validates values (email, number ranges,
  etc.) before saving.
- **Edit** — open a record and change it. Some fields may be read-only (set by a
  workflow or your permissions).
- **Clone** — use **Clone** to start a new record pre-filled from an existing one.
- **Inline edit** — in a list, click an editable cell (e.g. a status or number)
  to change it in place without opening the form.

### Linking records

- A **drop-down** field links to another record (for example, an order's
  *Customer*). Some drop-downs are **dependent** — choosing one field narrows the
  options of another.
- A **many-to-many** picker lets you attach several related records at once.

## The record page

Opening a record (the **View** action) shows its details as a read-only page, with
links to the records it references. Depending on how the table is set up, a record
page can also show:

- **Related records** — children of the record (e.g. a customer's orders) appear as
  tabbed **related lists**, each with an **Add** link that pre-fills the connection
  back to the parent. A **History** tab shows the change log on audited tables.
- **Impact map** — click **Impact map** to open a diagram of what this record depends
  on (upstream) and what depends on it (downstream — e.g. the devices linked to an
  item), out to a depth you choose. Drag to pan, scroll to zoom, click a node to
  recenter, ↗ to open the record.
- **SLA** — if the record is under a service-level target, an **SLA** panel shows its
  status (on-track / due-soon / paused / met / breached) and the due time.
- **Approvals** — if a status change needs sign-off, an **Approvals** panel shows the
  pending request, the decision trail, and (if you're an approver) Approve / Reject
  buttons — see below.

Fields of the **Markdown** type render formatted (headings, lists, links, code)
on the record page; you write plain markdown in the form.

## Files and images

Where a designer added a **file** or **image** field, you can upload from the
form. Images show a thumbnail on the view page; files show a download link.

## Deleting and the Trash

- **Delete** removes a record. If the table uses **soft delete**, the record goes
  to the **Trash** instead of being erased.
- Open **Trash** (above the list) to **view** or **restore** deleted records.

## Bulk actions

Tick the checkboxes on several rows, then use the bar above the list:

- **Delete selected**
- **Export selected** to CSV
- **Send to tools** — push the selected rows to a connected tool (if your
  designer set up an integration). You can also send a single record from its
  view page.

## Importing data (CSV)

Use **Import data** (top of the menu) to load a CSV into a table:

1. Choose the table and pick the CSV file.
2. Choose a **mode** — *Insert new rows* or *Upsert* (update existing rows,
   matched on a key column).
3. Optionally tick *import valid rows even if some have errors*.

A column guide on the page shows the expected headers. Multi-value **tags**
import as a delimited cell like `red|green`.

## Other views

- **Kanban** — drag cards between columns of a status field to move records
  through their states.
- **Calendar** — see records by a date field on a month grid.

(These appear when a designer enabled them for a table.)

## Reports

Open a **report** to group records and see counts/sums/averages, often with a
chart. From a report you can:

- **Save** the report (name it) to reuse the grouping/filters later.
- **Pin** a saved report to your home page.
- **Email it on a schedule** — when saving, set *every N minutes* + recipients to
  receive the report as a recurring digest.
- **Add to dashboard** — drop the current report onto a dashboard as a chart or a
  single-number (KPI) tile.

## Dashboards

A **dashboard** is a page of tiles — charts, single-number KPIs, short lists, and
text notes.

- **Shared dashboards** a designer built appear in your left menu.
- **Your own:** open **My dashboards** (top of the menu) to create personal
  dashboards. Add chart/KPI tiles from a report's **Add to dashboard** button, and
  list/text tiles on the dashboard itself. Only you see your personal dashboards.

## Service catalog & My requests

If your team publishes a **Catalog** (the link appears near the top of the menu
once it has content), it lists request cards —
*new laptop*, *report an incident*, … Submitting one creates a record the responsible
team works on. Track everything you've submitted under **My requests** (type, status
chip, created date, link to the record).

## Notifications

The **🔔 bell** shows unread notifications (for example, an item assigned to you
by a workflow rule). Open it to read and clear them.

## Approvals

Some status changes are routed for **approval** before they take effect. When you set
such a status, the record stays put and the change is **submitted for approval**.

- Approvers see pending items under **Approvals** (the **✓** badge in the top bar opens
  the approvals inbox) and on each record's **Approvals** panel.
- **Approve** or **Reject** with an optional comment. When every required approver has
  signed off the change is applied; any rejection cancels it. Steps may be sequential
  (one after another) or parallel (several must approve).

## API tokens

If you integrate Biggy with other software, open **API tokens** (top of the menu)
to create a personal bearer token. The secret is shown **once** on creation —
copy it then. You can revoke a token at any time. The same page links to the
**API reference** (`/api/v1/docs`) — a self-describing list of endpoints you can
also import into Postman/Swagger.

## Keyboard shortcuts

Common actions have shortcuts (for example **n** for new record, **/** to focus
search). Hover a button to see its shortcut, where available.

---

Need to build tables, forms, or menus yourself? See the **Designer manual**.
