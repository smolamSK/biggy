# Biggy — User Manual

A quickstart for **User mode**: entering, finding, and managing data using the
forms and menus a designer has set up for you. (If you also build the data model,
see the **Designer manual**.)

## Signing in

Open the app and sign in with the username and password you were given. After
signing in you land on **Home**.

If you ever need to change your password, open the **account** link (your
username, top-right) → *Change password*.

## The screen at a glance

- **Top bar** — the Biggy brand, a **theme** picker (Light / Dark / Sepia / Ocean
  / High contrast), the **🔔 notifications** bell, **Help**, your account, and
  **Sign out**.
- **Left menu** — the navigation a designer built: groups you can expand/collapse,
  and links that open a **list** of records. A **Search all…** box at the top
  searches across everything you can see.
- **Main area** — whatever you opened (a list, a record, a report…).

## Opening a list

Click a menu entry to open a **list** of records (for example *Customers*). From
a list you can:

- **Search** — type in the search box to match text across columns.
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

## The record page and related records

Opening a record (the **View** action) shows its details as a read-only page,
with links to the records it references. If the record has children (for example
a customer's orders), they appear as **related lists** with an **Add** link that
pre-fills the connection back to the parent.

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

## Notifications

The **🔔 bell** shows unread notifications (for example, an item assigned to you
by a workflow rule). Open it to read and clear them.

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
