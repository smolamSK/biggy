"""Read-only schema dependency reports; no DDL or configuration mutations."""
import json
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode

from flask import url_for
from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from . import data_service, form_rules
from .db import engine_for_table
from .metadata.models import (
    Attachment,
    CompositeUnique,
    Feed,
    FormDraft,
    MetaField,
    MetaFieldPermission,
    MetaForm,
    MetaMenu,
    MetaPermission,
    MetaRelation,
    PullSource,
    RecurringRecord,
    ReportDef,
    SlaPolicy,
    TriggerRule,
    Webhook,
    Workflow,
)


def dependencies(session, table, field=None, relation=None):
    whole_table = field is None and relation is None
    fields = [field] if field else list(table.fields) if whole_table else []
    ids = {f.id for f in fields}
    names = {f.phys_name for f in fields}
    relations = session.scalars(select(MetaRelation).where(or_(
        MetaRelation.from_table_id == table.id, MetaRelation.to_table_id == table.id))).all()
    relevant = [r for r in relations if whole_table or r.from_field_id in ids or
                relation is not None and r.id == relation.id]
    rel_ids = {r.id for r in relevant}
    incoming = session.scalars(select(MetaField).where(MetaField.related_table_id == table.id)).all() if whole_table else []
    incoming_ids = {f.id for f in incoming}
    groups, notes = defaultdict(list), []

    def add(group, label, reason, endpoint, query=None, **args):
        entry = {'label': label, 'reason': reason, 'url': url_for(endpoint, **args)}
        if query:
            entry['url'] += '?' + urlencode([(k, v) for k, v in parse_qsl(query) if k != 'export'])
        if entry not in groups[group]:
            groups[group].append(entry)

    def uses(items, form_table_id):
        reasons = set()
        for item in items:
            if item.get('field_id') in incoming_ids:
                reasons.add('Selects related records from this table')
            if item.get('field_id') in ids:
                reasons.add('Displays this field')
            if item.get('parent_field_id') in ids or item.get('filter_field_id') in ids:
                reasons.add('Uses this field to filter a dependent dropdown')
            if item.get('relation_id') in rel_ids:
                reasons.add('Displays related records')
            if form_table_id == table.id and names:
                try:
                    rules = form_rules.parse(item.get('rules_json'))
                    if any(form_rules.references(tree) & names for tree in rules.values()):
                        reasons.add('Uses this field in a conditional rule')
                except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
                    reasons.add('Contains unreadable conditions; review manually')
        return reasons

    forms = session.scalars(select(MetaForm).order_by(MetaForm.title)).all()
    form_map = {f.id: f for f in forms}
    affected_forms = set()
    for form in forms:
        items = [{key: getattr(i, key) for key in (
            'field_id', 'relation_id', 'parent_field_id', 'filter_field_id', 'rules_json')}
                 for i in form.items]
        reasons = uses(items, form.table_id)
        if whole_table and form.table_id == table.id:
            reasons.add('Collects or displays records from this table')
        if form.table_id == table.id and form.default_sort in names:
            reasons.add('Sorts records by this field')
        if reasons:
            affected_forms.add(form.id)
            add('Live forms', form.title, '; '.join(sorted(reasons)),
                'designer.form_edit', form_id=form.id)
    for draft in session.scalars(select(FormDraft)):
        form = form_map.get(draft.form_id)
        if not form:
            continue
        try:
            config = json.loads(draft.config_json)
            reasons = uses(config['items'], form.table_id)
        except (ValueError, TypeError, KeyError, AttributeError):
            notes.append(f'Draft for {form.title} could not be inspected.')
            continue
        if whole_table and form.table_id == table.id:
            reasons.add('Draft belongs to a form on this table')
        if reasons:
            add('Saved drafts', form.title, '; '.join(sorted(reasons)),
                'studio.builder', form_id=form.id)

    for rel in relevant:
        if relation is None or rel.id != relation.id:
            add('Relationships', rel.name, 'Connects records through this table or field',
                'designer.relation_edit', relation_id=rel.id)
    if whole_table:
        for link in incoming:
            add('Incoming pickers', f'{link.table.label} · {link.label}', 'Selects records from this table',
                'designer.table_view', table_id=link.table_id)
    if table.display_field_id in ids:
        add('Labels and constraints', table.label, 'Default label for related-record pickers',
            'designer.table_view', table_id=table.id)
    for rel in relations:
        for key in ('from_display_field_ids', 'to_display_field_ids'):
            try:
                matches = ids.intersection(json.loads(getattr(rel, key) or '[]'))
            except (ValueError, TypeError):
                notes.append(f'Picker labels for {rel.name} could not be inspected.')
                continue
            if matches:
                add('Labels and constraints', rel.name, 'Uses the field in picker labels',
                    'designer.relation_edit', relation_id=rel.id)
    for unique in session.scalars(select(CompositeUnique).where(CompositeUnique.table_id == table.id)):
        try:
            matches = whole_table or ids.intersection(json.loads(unique.field_ids))
        except (ValueError, TypeError):
            notes.append(f'Constraint {unique.name} could not be inspected.')
            continue
        if matches:
            add('Labels and constraints', unique.name, 'Enforces uniqueness across these fields',
                'designer.table_view', table_id=table.id)
    for permission in session.scalars(select(MetaFieldPermission).where(MetaFieldPermission.field_id.in_(ids))):
        add('Permissions', permission.role, f'Field access: {permission.access}',
            'designer.field_permissions', table_id=table.id)
    for permission in session.scalars(select(MetaPermission).where(MetaPermission.form_id.in_(affected_forms))):
        add('Permissions', f'{permission.role} · {form_map[permission.form_id].title}',
            f'Access to an affected form: {permission.access}', 'designer.permissions')
    for menu in session.scalars(select(MetaMenu)):
        if menu.target_form_id in affected_forms or whole_table and menu.target_table_id == table.id:
            add('Navigation', menu.label, 'Opens affected records or a form', 'designer.menus')
    for workflow in session.scalars(select(Workflow).where(Workflow.table_id == table.id)):
        if whole_table or workflow.field_id in ids:
            add('Workflows', f'Workflow {workflow.id}', 'Controls transitions of this status field',
                'designer.workflow_edit', wf_id=workflow.id)

    # These entries are intentionally table-level review candidates, not claims of
    # exact column references hidden inside mappings, templates or expressions.
    if whole_table or field:
        candidates = [
            (TriggerRule, or_(TriggerRule.table_id == table.id, TriggerRule.create_table_id == table.id),
             'Automations to review', 'designer.trigger_edit', 'rule_id'),
            (Feed, Feed.source_table_id == table.id, 'Integrations to review', 'designer.feed_edit', 'feed_id'),
            (Webhook, Webhook.target_table_id == table.id, 'Integrations to review', 'designer.webhook_edit', 'webhook_id'),
            (PullSource, PullSource.target_table_id == table.id, 'Integrations to review', 'designer.pull_edit', 'source_id'),
            (SlaPolicy, SlaPolicy.table_id == table.id, 'Automations to review', 'designer.sla_policy_edit', 'policy_id'),
            (RecurringRecord, RecurringRecord.table_id == table.id, 'Automations to review', 'designer.recurring_home', None),
            (ReportDef, ReportDef.table_id == table.id, 'Reports to review', 'user.report', 'table_id'),
        ]
        for model, clause, group, endpoint, arg in candidates:
            for obj in session.scalars(select(model).where(clause)):
                args = {arg: table.id if model is ReportDef else obj.id} if arg else {}
                add(group, obj.name, 'Uses this table; review its fields, conditions and mappings', endpoint, query=obj.query if model is ReportDef else None, **args)
    neighbor_ids = {table.id} | {r.from_table_id for r in relations} | {r.to_table_id for r in relations}
    for formula in session.scalars(select(MetaField).where(
            MetaField.table_id.in_(neighbor_ids), MetaField.data_type == 'formula')):
        add('Formulas to review', f'{formula.table.label} · {formula.label}',
            'Formula on this or a connected table; review field references, lookups and rollups',
            'designer.table_view', table_id=formula.table_id)
    return [{'title': title, 'entries': entries} for title, entries in groups.items()], sorted(set(notes))


def record_counts(session, table, field=None, relation=None):
    """Explicitly requested counts include all records, including soft-deleted rows."""
    result = {'rows': None, 'populated': None, 'attachments': None, 'error': None}
    ids = [field.id] if field else [f.id for f in table.fields] if relation is None else []
    if ids:
        result['attachments'] = session.scalar(select(func.count()).select_from(Attachment)
                                               .where(Attachment.field_id.in_(ids)))
    try:
        physical = relation.junction_phys_name if relation and relation.kind == 'mn' else table.phys_name
        engine = engine_for_table(table)
        reflected = data_service.reflect_table(engine, physical)
        columns = [func.count()]
        if field and field.data_type not in {'file', 'image'}:
            columns.append(func.count(reflected.c[field.phys_name]))
        with engine.connect() as conn:
            row = conn.execute(select(*columns).select_from(reflected)).one()
        result['rows'] = row[0]
        if len(row) > 1:
            result['populated'] = row[1]
    except (SQLAlchemyError, OSError, ValueError, KeyError):
        result['error'] = 'Record counts are unavailable. Check the data source connection and physical schema.'
    return result
