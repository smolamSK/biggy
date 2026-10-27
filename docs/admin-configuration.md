# Configure and publish forms

## Stage 1: Set up and check

Open **Designer → Setup checklist**. Define your tables and fields, connect related
records, build forms, configure role permissions, and add menu entries or catalog
cards. Checklist badges indicate configuration exists; they do not certify that
permissions or business requirements have been reviewed.

The relationship screen explains which records belong to which table and suggests
editable names. Review deletion behavior before creating the relationship: this
operation changes the database immediately. Configuration health identifies missing
required fields, broken or ambiguous dependent pickers, dependency cycles,
required fields that roles cannot write, and unreachable workflow states.

## Stage 2: Build and test behavior

Open **Designer → Forms**, then select a form. Add fields, related-record pickers
and sections. Drag controls to reorder them, or use their keyboard-accessible
Move up/Move down buttons. Select a control to edit its label, help text and rules.

For example, select **Resolution** and add **Require when → Status equals Closed**.
Use nested **All conditions (AND)** and **Any condition (OR)** groups for more
complex conditions. Show, require and read-only rules run in the browser and are
validated again by the server. Dependencies cannot form a cycle.

Hidden or read-only values are preserved when editing. Submitted replacements are
ignored. On creation, hidden fields are omitted so database/default handling can
apply. A database-required field without a default cannot be conditionally hidden
or read-only. Uploads and generated fields do not support conditional rules.

For dependent dropdowns, choose a controlling relationship field and a matching
relationship in the target table. Automatic matching works when exactly one match
exists. An empty parent allows all choices; a selected parent filters choices.
The server also rejects a selected child that belongs to a different parent.

Save the draft, choose **Test as a role**, and run the preview. Enter sample values
and run it again to exercise different branches. Select an active account to test
organization-specific choices. The preview explains visible, hidden, required and
read-only fields, validation errors, and matching relationship choices. It displays
at most 100 options per picker and reports the total count. It never creates records,
uploads files or saves relationship links. Test uploads in your isolated preview
environment if they are part of the workflow.

Rules apply to staff and portal form submissions. They are form behavior, not access
permissions or universal data constraints. API/import paths continue to use their
own validation; use database constraints and workflows for universal invariants.
Inline and bulk field editing are disabled for forms with conditional rules; use the
full form so its conditions can be evaluated together.

## Stage 3: Review and publish

Saving a draft leaves the live form unchanged. Review the change comparison and
resolve configuration errors. A successful preview of the saved draft is required
before **Publish saved draft** becomes available. Test representative roles and
both matching and nonmatching sample values, rather than relying on one example.

Publishing updates the form in one transaction, records its previous and new
versions, and writes an audit event. It does not rewrite existing records or change
database columns. A concurrent draft save is rejected; reload to obtain the latest
version. If the live layout or field schema changes, start fresh and review again.

**Previous versions → Restore to draft** copies a saved configuration into the
draft. Test and publish it to make it live. Revision restoration does not restore
records or database schema. Drafts are shared by designers, not private per user.

**Live settings** retains the existing editor for advanced settings. Changes there
apply immediately and can make an open draft stale. Table and relationship schema
changes also remain immediate; the draft publishing workflow covers form layouts,
catalog settings and conditional behavior. Schema exports include published rules;
drafts and publication history are local metadata and are not exported.

## Stage 4: Inspect schema impact

Use **Schema impact** on a table, or **Impact** next to a field or relationship.
The report links to live forms, saved drafts, conditional rules, dependent pickers,
labels, constraints, permissions, navigation and workflows that reference it.
Automations, integrations, reports and formulas on affected or connected tables
are listed separately as review candidates; a candidate may use other fields.

Choose **Count existing records** to request current counts. Table and field
counts include every owner and soft-deleted rows; populated-field counts mean
non-NULL values, including empty strings. A many-to-many relationship reports its
saved links. Attachments are counted separately. If a physical data source is
unavailable, configuration references remain visible and counts show unavailable.

The report describes deletion behavior for managed tables, external mappings,
fields and relationships. It reads metadata and optionally counts records; it does
not alter the schema, create a form draft, or execute integrations. Historical
revisions, saved user filters, dashboard widgets, raw SQL and external API clients
are not fully analyzed. Use the report to review dependencies, not as a migration
simulation or a guarantee that every existing value meets proposed constraints.

## Stage 5: Reuse templates and follow guided setup

Open **Designer → Form templates** (also linked from **Forms**). The library
includes service-request, asset-register and approval-request starters. Expand
**Fields needed** to see each starter's field types and required enum choices.
Starters are form layouts and conditional behavior; the approval starter does not
install approval routing or grant anyone permission to make decisions.

To save your own template, open a form's builder and choose **Save as template**.
Choose its current live configuration or its saved draft, then enter a name and
description. Save builder changes first: unsaved edits are not captured. A template
keeps layout, sections, labels, help text, required/read-only settings, conditional
rules and relationship requirements. It is independent of the source form and
continues working if that form changes or is deleted.

Choose **Use template** and follow the wizard:

1. **Choose form.** Select an existing target form, or create a form first and
   return to the library. The wizard uses existing table fields; it does not create
   tables, columns or relationships.
2. **Map fields.** Match every template field to a different target field. Types
   must match, and enum choices must include the template's choices. Matching names
   are suggested. Map many-to-many relationships and dependent-dropdown match fields
   when present. Missing fields can be added through the linked table editor; reload
   the wizard afterwards.
3. **Review conditions.** Read the mapped show/require/read-only behavior and any
   configuration issues. Field references are remapped; comparison values remain
   unchanged, so check any conditions containing related-record IDs. Conditions and
   layout can be adjusted in the builder after creating the draft.
4. **Review access.** Inspect the target form's current role settings and field
   restrictions. Templates never copy permissions, catalog visibility, approval
   routing or record data. Permission links open the existing editors, whose changes
   apply immediately; review again if you change access.
5. **Create draft and test.** If there is an edited saved draft, explicitly choose
   whether to replace it. The wizard creates only a draft and opens role preview.
   Test matching and nonmatching scenarios, fix configuration issues, then publish
   through the existing builder.

Review expires after one hour. Changes to the target form, draft, mappings or
permissions require a fresh review before applying. The target form keeps its title,
name and catalog settings; the template supplies description, purpose, controls and
conditions. Deleting a template keeps forms and drafts created from it.

Saved templates are local application metadata. They are not included in schema
JSON exports; include the metadata database in backups to preserve the library.
