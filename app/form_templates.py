"""Portable form layouts mapped to existing schema before becoming drafts."""
import copy
import json

from sqlalchemy import select

from . import form_design, form_rules
from .metadata.models import MetaRelation, MetaTable


def capture(session, form, config):
    config = copy.deepcopy(config)
    form_design.validate_config(session, form, config)
    field_map = {f.id: f for f in form.table.fields}
    fields, relations, items = {}, {}, []
    for item in config['items']:
        row = {key: item.get(key) for key in (
            'kind', 'label_override', 'help_text', 'required', 'readonly', 'rules_json', 'widget')}
        if item['kind'] == 'field':
            field = field_map[item['field_id']]
            row['field'] = field.phys_name
            fields[field.phys_name] = {
                'label': field.label, 'type': field.data_type,
                'choices': json.loads(field.enum_options or '[]') if field.data_type == 'enum' else [],
            }
            if item.get('parent_field_id'):
                parent = field_map.get(item['parent_field_id'])
                if not parent:
                    raise ValueError('Fix the dependent dropdown before saving a template.')
                row['parent'] = parent.phys_name
                target = session.get(MetaTable, field.related_table_id)
                match = next((f for f in target.fields if f.id == item.get('filter_field_id')), None) if target else None
                row['filter'] = match.phys_name if match else None
        elif item['kind'] == 'relation':
            relation = session.get(MetaRelation, item['relation_id'])
            key = f'relation_{len(relations)}'
            relations[key] = {'label': relation.name}
            row['relation'] = key
        items.append(row)
    return {'version': 1, 'description': config['description'], 'purpose': config['purpose'],
            'fields': fields, 'relations': relations, 'items': items}


def starter(description, specifications):
    fields, items = {}, []
    for name, label, dtype, choices, rules in specifications:
        fields[name] = {'label': label, 'type': dtype, 'choices': choices}
        items.append({'kind': 'field', 'field': name, 'label_override': label,
                      'required': name in {'summary', 'asset_name', 'request'}, 'readonly': False,
                      'rules_json': form_design.dump(rules) if rules else None})
    return {'version': 1, 'description': description, 'purpose': 'data',
            'fields': fields, 'relations': {}, 'items': items}


BUILTINS = {
    'service-request': {'name': 'Service request', 'description': 'Summary, priority and status, with resolution required when closed.',
        'config': starter('Describe the request and track its resolution.', [
            ('summary', 'Summary', 'string', [], {}),
            ('priority', 'Priority', 'enum', ['Low', 'Normal', 'High'], {}),
            ('status', 'Status', 'enum', ['Open', 'In progress', 'Closed'], {}),
            ('resolution', 'Resolution', 'text', [], {'require': {'field': 'status', 'op': 'eq', 'value': 'Closed'}}),
        ])},
    'assets': {'name': 'Asset register', 'description': 'Name, serial number and status, with retirement details required when retired.',
        'config': starter('Register an asset and track its lifecycle.', [
            ('asset_name', 'Asset name', 'string', [], {}),
            ('serial_number', 'Serial number', 'string', [], {}),
            ('status', 'Status', 'enum', ['Available', 'Assigned', 'Retired'], {}),
            ('retirement_reason', 'Retirement reason', 'text', [], {'require': {'field': 'status', 'op': 'eq', 'value': 'Retired'}}),
        ])},
    'approval-request': {'name': 'Approval request', 'description': 'Request, decision and rejection reason. Configure approval routing separately.',
        'config': starter('Record a request and its decision. Configure workflow roles and approval routing separately.', [
            ('request', 'Request', 'string', [], {}),
            ('decision', 'Decision', 'enum', ['Pending', 'Approved', 'Rejected'], {}),
            ('reason', 'Rejection reason', 'text', [], {'require': {'field': 'decision', 'op': 'eq', 'value': 'Rejected'}}),
        ])},
}


def compatible(spec, field):
    if spec['type'] != field.data_type:
        return False
    if field.data_type == 'enum':
        return set(spec['choices']).issubset(json.loads(field.enum_options or '[]'))
    return True


def choices(session, form, template):
    result = {}
    for key, spec in template['fields'].items():
        result[key] = [{'id': f.id, 'name': f.phys_name, 'label': f.label}
                       for f in form.table.fields if compatible(spec, f)]
    relations = session.scalars(select(MetaRelation).where(
        MetaRelation.kind == 'mn',
        (MetaRelation.from_table_id == form.table_id) | (MetaRelation.to_table_id == form.table_id))).all()
    return result, [{'id': r.id, 'label': r.name} for r in relations]


def map_config(session, form, template, field_mapping, relation_mapping, filters):
    if template.get('version') != 1:
        raise ValueError('This template version is not supported.')
    for mapping in (field_mapping, relation_mapping, filters):
        if not isinstance(mapping, dict):
            raise ValueError('Choose field and relationship mappings.')
    if set(field_mapping) != set(template['fields']) or set(relation_mapping) != set(template['relations']):
        raise ValueError('Map every template field and relationship.')
    fields = {f.id: f for f in form.table.fields}
    mapped = {}
    for key, spec in template['fields'].items():
        fid = field_mapping[key]
        if type(fid) is not int or fid not in fields or not compatible(spec, fields[fid]):
            raise ValueError(f'{spec["label"]}: choose a compatible field on the target table.')
        mapped[key] = fields[fid]
    if len({f.id for f in mapped.values()}) != len(mapped):
        raise ValueError('Map each template field to a different target field.')
    if len(set(relation_mapping.values())) != len(relation_mapping):
        raise ValueError('Map each relationship to a different target relationship.')
    for rid in relation_mapping.values():
        if type(rid) is not int:
            raise ValueError('Choose a valid relationship.')
        relation = session.get(MetaRelation, rid)
        if not relation or relation.kind != 'mn' or form.table_id not in (relation.from_table_id, relation.to_table_id):
            raise ValueError('Choose a many-to-many relationship involving the target table.')

    def remap(node):
        if 'field' in node:
            if node['field'] not in mapped:
                raise ValueError('A condition refers to a field missing from the template.')
            node['field'] = mapped[node['field']].phys_name
        else:
            for child in node.get('all', node.get('any', [])):
                remap(child)

    config = form_design.snapshot(form)
    # Keep the target's identity and catalog exposure; permissions never travel.
    config.update(description=template['description'], purpose=template['purpose'], items=[])
    for source in template['items']:
        item = {key: None for key in form_design.ITEM_KEYS}
        for key in ('kind', 'label_override', 'help_text', 'required', 'readonly', 'widget'):
            item[key] = source.get(key)
        if source['kind'] == 'field':
            field = mapped[source['field']]
            item['field_id'] = field.id
            rules = copy.deepcopy(form_rules.parse(source.get('rules_json')))
            for tree in rules.values():
                remap(tree)
            item['rules_json'] = form_design.dump(rules) if rules else None
            if source.get('parent'):
                parent = mapped[source['parent']]
                item['parent_field_id'] = parent.id
                target = session.get(MetaTable, field.related_table_id)
                candidates = [f for f in target.fields if f.data_type == 'relation'
                              and f.related_table_id == parent.related_table_id] if target else []
                match_id = filters.get(source['field'])
                if match_id is not None:
                    if type(match_id) is not int or match_id not in {f.id for f in candidates}:
                        raise ValueError(f'{field.label}: choose a valid dependent-dropdown match field.')
                else:
                    same_name = [f for f in candidates if f.phys_name == source.get('filter')]
                    match = same_name[0] if len(same_name) == 1 else candidates[0] if len(candidates) == 1 else None
                    if not match:
                        raise ValueError(f'{field.label}: choose its dependent-dropdown match field.')
                    match_id = match.id
                item['filter_field_id'] = match_id
        elif source['kind'] == 'relation':
            item['relation_id'] = relation_mapping[source['relation']]
        config['items'].append(item)
    issues = form_design.validate_config(session, form, config)
    return config, issues
