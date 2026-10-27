"""Declarative form rules shared by the designer, previews and server validation.

Rules are form UX/validation, not permissions. Hidden values are always preserved.
"""
import json
from decimal import Decimal, InvalidOperation

NUMERIC = {'integer', 'bigint', 'float', 'decimal', 'currency', 'percent'}
OPS = {'eq', 'ne', 'empty', 'not_empty', 'contains', 'gt', 'gte', 'lt', 'lte'}


def parse(value):
    rules = json.loads(value) if isinstance(value, str) and value else (value or {})
    if not isinstance(rules, dict) or set(rules) - {'show', 'require', 'readonly'}:
        raise ValueError('Rules must contain only show, require or readonly conditions.')
    return rules


def references(node):
    if not node:
        return set()
    if 'field' in node:
        return {node['field']}
    return set().union(*(references(c) for c in node.get('all', node.get('any', []))))


def validate(rules, fields, target):
    def visit(node, depth=0):
        if depth > 5 or not isinstance(node, dict):
            raise ValueError('Condition groups may be nested at most five levels.')
        if 'all' in node or 'any' in node:
            key = 'all' if 'all' in node else 'any'
            children = node[key]
            if set(node) != {key} or not isinstance(children, list) or not 1 <= len(children) <= 20:
                raise ValueError('Each AND/OR group needs 1–20 conditions.')
            for child in children:
                visit(child, depth + 1)
        else:
            name, op = node.get('field'), node.get('op')
            if name not in fields or name == target or op not in OPS:
                raise ValueError('Choose another field on this form and a supported operator.')
            dtype = fields[name].data_type
            if dtype in {'file', 'image', 'formula', 'autonumber', 'json', 'tags'}:
                raise ValueError('This field type cannot control a condition.')
            if op in {'gt', 'gte', 'lt', 'lte'} and dtype not in NUMERIC:
                raise ValueError('Numeric comparisons require a numeric field.')
            if op == 'contains' and dtype not in {'string', 'text', 'markdown', 'email', 'url', 'phone'}:
                raise ValueError('Contains requires a text field.')
            value = node.get('value', '')
            if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 1000:
                raise ValueError('Condition values must be short scalar values.')
            if op not in {'empty', 'not_empty'} and dtype in NUMERIC:
                try:
                    if not Decimal(str(value)).is_finite():
                        raise ValueError('Use a finite number.')
                except InvalidOperation:
                    raise ValueError('Use a numeric condition value.') from None
    for tree in parse(rules).values():
        visit(tree)


def text(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def matches(node, values, types=None):
    if 'all' in node:
        return all(matches(c, values, types) for c in node['all'])
    if 'any' in node:
        return any(matches(c, values, types) for c in node['any'])
    name, op = node['field'], node['op']
    if name not in values:
        return False  # hidden by permissions: never infer its value
    left, right = text(values[name]), text(node.get('value', ''))
    if op == 'empty':
        return left == ''
    if op == 'not_empty':
        return left != ''
    if op == 'contains':
        return right in left
    if (types or {}).get(name) in NUMERIC:
        try:
            left, right = Decimal(left), Decimal(right)
            if not left.is_finite() or not right.is_finite():
                return False
        except InvalidOperation:
            return False
    return {'eq': lambda: left == right, 'ne': lambda: left != right,
            'gt': lambda: left > right, 'gte': lambda: left >= right,
            'lt': lambda: left < right, 'lte': lambda: left <= right}[op]()


def order(config):
    result, active = [], set()
    def visit(name):
        if name in active:
            raise ValueError('Conditional rules form a cycle.')
        if name in result:
            return
        active.add(name)
        for tree in config[name].get('rules', {}).values():
            for dependency in references(tree):
                if dependency in config:
                    visit(dependency)
        active.remove(name)
        result.append(name)
    for name in config:
        visit(name)
    return result


def states(config, values, trusted=None):
    values = dict(values)
    trusted = trusted or {}
    types = {name: item['type'] for name, item in config.items()}
    result = {}
    for name, item in config.items():
        if item.get('locked'):
            values[name] = trusted.get(name)
    for name in order(config):
        item = config[name]
        rules = item.get('rules', {})
        visible = matches(rules['show'], values, types) if 'show' in rules else True
        locked = item.get('locked', False) or (matches(rules['readonly'], values, types) if 'readonly' in rules else False)
        required = item.get('required', False) or (matches(rules['require'], values, types) if 'require' in rules else False)
        result[name] = {'visible': visible, 'readonly': locked, 'required': required and visible and not locked}
        if not visible or locked:
            values[name] = trusted.get(name)
    return result


def bind(form_class, items, definitions, session, user=None):
    """Wrap generated WTForms classes; permissions and hidden values are immutable."""
    from wtforms.validators import InputRequired, Optional
    by_field = {d.field_id: d for d in definitions if d.kind == 'field'}
    config = {}
    for item in items:
        if item.kind not in {'field', 'relation_m1', 'file'}:
            continue
        definition = by_field.get(item.meta.id)
        if not definition:
            continue
        item.rules = parse(getattr(definition, 'rules_json', None))
        config[item.name] = {'rules': item.rules, 'type': item.meta.data_type,
                             'locked': bool(item.readonly),
                             'required': bool(definition.required or not item.meta.nullable)}
    has_rules = any(c['rules'] for c in config.values())

    class ConditionalForm(form_class):
        def __init__(self, *args, trusted_values=None, **kwargs):
            super().__init__(*args, **kwargs)
            self.rule_config = config if has_rules else {}
            initial = dict(trusted_values or kwargs.get('data') or {})
            from .importer import coerce_value
            from .record_service import _DEFAULT_TOKENS, _eval_default_token
            for item in items:
                if item.name not in config or item.name in initial or item.meta.data_type in {'formula', 'autonumber'}:
                    continue
                default = item.meta.default_value
                if default not in (None, ''):
                    try:
                        initial[item.name] = (_eval_default_token(session, item.meta, getattr(user, 'id', None))
                                              if default.strip().lower() in _DEFAULT_TOKENS
                                              else coerce_value(item.meta, default))
                    except ValueError:
                        pass
            self.rule_trusted = {name: (initial[name] if isinstance(initial[name], (str, bool, type(None)))
                                         else text(initial[name])) for name in config if name in initial}
            values = {name: field.data for name, field in self._fields.items()}
            self.rule_states = states(config, values, self.rule_trusted)
            for item in items:
                state = self.rule_states.get(item.name)
                if state:
                    item.hidden = not state['visible']
                    item.readonly = state['readonly'] or item.hidden
                    field = self._fields.get(item.name)
                    if field is not None and has_rules:
                        field.validators = [v for v in field.validators if not isinstance(v, (InputRequired, Optional))]
                        field.validators.insert(0, InputRequired() if state['required'] else Optional())
                        field.flags.required = state['required']

        def validate(self, extra_validators=None):
            removed = {}
            if has_rules:
                for name, state in self.rule_states.items():
                    if not state['visible'] or state['readonly']:
                        field = self._fields.pop(name, None)
                        if field is not None:
                            removed[name] = field
            try:
                valid = super().validate(extra_validators)
            finally:
                self._fields.update(removed)
            for name, field in self._fields.items():
                parent = (field.render_kw or {}).get('data-parent-field')
                if not parent or name in removed or not field.data or parent not in self._fields:
                    continue
                parent_state = self.rule_states.get(parent, {})
                parent_value = (self.rule_trusted.get(parent) if parent_state.get('readonly')
                                or parent_state.get('visible') is False else self._fields[parent].data)
                choice = next((c for c in field.choices if text(c[0]) == text(field.data)), None)
                if parent_value not in (None, '') and choice and len(choice) > 2:
                    expected = choice[2].get('data-parent')
                    if expected not in (None, '') and text(expected) != text(parent_value):
                        field.errors = list(field.errors) + ['Choose a record belonging to the selected parent.']
                        valid = False
            return valid
    return ConditionalForm
