"""Read-only configuration diagnostics for administrators."""
import json

from sqlalchemy import select

from . import form_rules
from .access import can_write, field_access, form_access
from .metadata.models import (
    AppUser,
    MetaField,
    MetaForm,
    MetaMenu,
    MetaRelation,
    MetaTable,
    Role,
    Workflow,
)


def check_form(session, form):
    issues = []
    fields = {f.id: f for f in form.table.fields}
    present = {i.field_id for i in form.items if i.kind == 'field'}
    edges = {}

    def issue(message, item=None, level='error'):
        issues.append({'message': message, 'level': level, 'form_id': form.id,
                       'item_id': getattr(item, 'id', None)})

    for item in form.items:
        field = fields.get(item.field_id)
        if item.kind == 'field' and not field:
            issue('A form item refers to a missing field.', item)
        if item.kind == 'relation':
            relation = session.get(MetaRelation, item.relation_id)
            if not relation or form.table_id not in (relation.from_table_id, relation.to_table_id):
                issue('A many-to-many item has a missing or unrelated relationship.', item)
        if not field:
            continue
        if item.parent_field_id:
            parent = fields.get(item.parent_field_id)
            target = session.get(MetaTable, field.related_table_id) if field.related_table_id else None
            matches = [f for f in target.fields if parent and f.related_table_id == parent.related_table_id
                       and f.data_type == 'relation'] if target else []
            if not parent or parent.id not in present or parent.data_type != 'relation':
                issue(f'{field.label}: its controlling field must be a relationship on this form.', item)
            elif item.filter_field_id:
                if item.filter_field_id not in {f.id for f in matches}:
                    issue(f'{field.label}: the match field does not link to the controlling table.', item)
            elif len(matches) != 1:
                issue(f'{field.label}: choose a match field; automatic matching is ambiguous or unavailable.', item)
            edges[field.id] = item.parent_field_id
        if field.data_type == 'relation' and not session.get(MetaTable, field.related_table_id):
            issue(f'{field.label}: the related table is missing.', item)
        if form.purpose != 'view' and (item.required or not field.nullable):
            if item.readonly and not field.default_value and field.data_type not in {'formula', 'autonumber'}:
                issue(f'{field.label} is required but read-only, with no default.', item)
            for role in session.scalars(select(Role)):
                if role.name == 'designer':
                    continue
                user = AppUser(role=role.name, is_active_flag=True)
                writable = can_write(form_access(session, user, form.id))
                if role.name == 'portal':
                    writable = bool(form.in_catalog)
                if writable and field_access(session, user, field) not in {'write', 'full'} and not field.default_value:
                    issue(f'{field.label} is required but unavailable to role “{role.label}”.', item)
    rule_fields = {f.phys_name: f for f in fields.values() if f.id in present}
    rule_config = {}
    for item in form.items:
        field = fields.get(item.field_id)
        if not field or item.kind != 'field':
            continue
        try:
            rules = form_rules.parse(getattr(item, 'rules_json', None))
            form_rules.validate(rules, rule_fields, field.phys_name)
            rule_config[field.phys_name] = {'rules': rules}
        except (ValueError, TypeError, KeyError):
            issue(f'{field.label}: conditional rules refer to an unavailable field or contain invalid conditions.', item)
    try:
        form_rules.order(rule_config)
    except ValueError:
        issue('Conditional rules form a cycle. Remove a circular dependency.')
    for start in edges:
        seen, node = set(), start
        while node in edges:
            if node in seen:
                issue('Dependent dropdowns form a cycle. Each chain must end at an independent field.')
                break
            seen.add(node)
            node = edges[node]
    if form.in_catalog and not (form.table.track_audit or form.table.row_owned):
        issue('This catalog form needs audit or ownership tracking before portal customers can use it.')
    for field in fields.values():
        if (form.purpose != 'view' and not field.nullable and field.id not in present
                and not field.default_value and field.data_type not in {'autonumber', 'formula'}
                and field.phys_name != form.table.pk_col):
            issue(f'Required field “{field.label}” is missing from the form.')
    return issues


def report(session):
    forms = session.scalars(select(MetaForm).order_by(MetaForm.title)).all()
    issues = [i for form in forms for i in check_form(session, form)]
    for wf in session.scalars(select(Workflow)):
        field = session.get(MetaField, wf.field_id)
        try:
            states = set(json.loads(field.enum_options or '[]')) if field else set()
            transitions = json.loads(wf.transitions or '[]')
            reachable = {wf.initial_state}
            while True:
                expanded = reachable | {t['to'] for t in transitions if t['from'] in reachable}
                if expanded == reachable:
                    break
                reachable = expanded
            missing = states - reachable
            if wf.initial_state not in states or missing:
                issues.append({'message': f'Workflow {wf.id}: invalid initial state or unreachable states: {", ".join(sorted(missing))}.',
                               'level': 'warning', 'workflow_id': wf.id})
        except (TypeError, ValueError, KeyError):
            issues.append({'message': f'Workflow {wf.id} has invalid transition data.', 'level': 'error', 'workflow_id': wf.id})
    tables = session.scalars(select(MetaTable)).all()
    steps = [
        ('Define tables and fields', bool(tables) and all(t.fields for t in tables), 'designer.table_new', 'Create the information your team will collect.'),
        ('Connect related records', bool(session.scalar(select(MetaRelation.id).limit(1))), 'designer.relations', 'Optional for standalone tables.'),
        ('Build forms', bool(forms) and all(f.items for f in forms), 'designer.forms', 'Choose fields, sections and rules.'),
        ('Check configuration', not issues, 'designer.setup_guide', 'Resolve errors before publishing.'),
        ('Configure roles', bool(session.scalar(select(Role.id).where(Role.name != 'designer').limit(1))), 'designer.permissions', 'Roles exist; review their form and field permissions before publishing.'),
        ('Make forms discoverable', bool(session.scalar(select(MetaMenu.id).limit(1))) or any(f.in_catalog for f in forms), 'designer.menus', 'Add a menu entry or catalog card.'),
    ]
    return steps, issues
