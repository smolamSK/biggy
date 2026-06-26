# Biggy example models

Ready-made models (schema **and** sample data) you can load into Biggy.

## Loading

- **One click (recommended):** in the app, go to **Designer → Examples** and click **Load** on a
  demo. This imports the schema and the sample rows (replacing the current model).
- **Manually:** in **Designer → Backup**, *Import schema* with `<name>.schema.json`, then *Import
  data* (tick *Replace existing*) with `<name>.data.json`.

Both files use Biggy's JSON export format (`version: 1`). A `*.schema.json` holds the
model in sections (`tables`, `fields`, `relations`, `forms`, `form_fields`, `menus`,
and — when used — `dashboards`, `workflows`, `trigger_rules`, `connections`, `feeds`,
`webhooks`, `pull_sources`, `permissions`, …); a `*.data.json` holds rows as
`{tables: {table_name: [rows…]}}`. They are regenerated with
`flask --app run dump-examples examples`.

These demos are deliberately **minimal domain apps** (tables/fields/relations/forms/
menus). For the **full format** — every section, the id-reference rule, field types,
and an LLM authoring guide — see **[`docs/schema-json-format.md`](../docs/schema-json-format.md)**
and the complete, importable template
**[`docs/schema-reference.example.json`](../docs/schema-reference.example.json)**.

## The demos

| File prefix | Model | Highlights |
|---|---|---|
| `cmdb` | **CMDB** | configuration items, environments, teams, applications; M:N *application ↔ CI* (runs-on); enums for type/status/criticality |
| `library` | **Library (book borrowing)** | authors, books, members, loans; M:1 chains; date fields; genre enum |
| `helpdesk` | **Helpdesk** | tickets with status/priority enums, categories, agents; long-text description |
| `crm` | **CRM / sales** | companies, contacts, deals; decimal amount; sales-stage enum |
| `projects` | **Projects & tasks** | projects, tasks (status/priority), labels; M:N *task ↔ label* |
| `hr` | **HR / employees** | departments, employees; **manager self-relation** (employee → employee); position enum |
| `netcmdb` | **Network CMDB** | sites, devices, interfaces, links; a larger interconnected model with sample data |

Each demo also ships forms and a menu group, so after loading it's immediately usable in **User mode**.
