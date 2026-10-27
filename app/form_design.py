"""Draft form configuration, validation, optimistic concurrency and publishing."""
import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import select

from . import form_rules
from .design_health import check_form
from .metadata.models import (
    FormDraft,
    FormRevision,
    MetaField,
    MetaForm,
    MetaFormField,
    MetaRelation,
    MetaTable,
    SecurityEvent,
)

FORM_KEYS = ('title', 'description', 'purpose', 'in_catalog', 'catalog_group')
ITEM_KEYS = ('kind', 'field_id', 'relation_id', 'parent_field_id', 'filter_field_id',
             'label_override', 'help_text', 'required', 'readonly', 'rules_json', 'widget')


def dump(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def snapshot(form):
    return {**{key: getattr(form, key) for key in FORM_KEYS},
            'items': [{key: getattr(item, key, None) for key in ITEM_KEYS}
                      for item in sorted(form.items, key=lambda it: (it.position, it.id or 0))]}


def live_hash(form):
    schema = [{c.name: getattr(f, c.name) for c in MetaField.__table__.columns}
              for f in form.table.fields]
    table = {c.name: getattr(form.table, c.name) for c in MetaTable.__table__.columns
             if c.name != 'created_at'}
    return digest({'config': snapshot(form), 'schema': schema, 'table': table})


def virtual(form, config):
    items = [SimpleNamespace(id=None, form_id=form.id, position=pos,
                             **{key: item.get(key) for key in ITEM_KEYS})
             for pos, item in enumerate(config['items'])]
    return SimpleNamespace(id=form.id, table=form.table, table_id=form.table_id,
                           name=form.name, items=items, **{key: config.get(key) for key in FORM_KEYS})


def validate_config(session, form, config):
    if not isinstance(config, dict) or set(config) != set(FORM_KEYS) | {'items'}:
        raise ValueError('A draft must contain the form settings and its items.')
    for name, limit in [('title',160),('description',10000),('catalog_group',80)]:
        if config[name] is not None and (not isinstance(config[name], str) or len(config[name]) > limit):
            raise ValueError(f'{name} is too long or has the wrong type.')
    if not config['title'] or config['purpose'] not in {'data','view'} or not isinstance(config['in_catalog'], bool):
        raise ValueError('Choose a title, purpose and catalog setting.')
    if not isinstance(config['items'], list) or len(config['items']) > 200:
        raise ValueError('A form can have at most 200 items.')
    fields = {f.id: f for f in form.table.fields}
    names, seen = {}, set()
    for item in config['items']:
        if not isinstance(item, dict) or set(item) - set(ITEM_KEYS):
            raise ValueError('Unknown form item properties.')
        if item.get('kind') not in {'field','relation','section'}:
            raise ValueError('Unknown form item type.')
        for key in ('field_id','relation_id','parent_field_id','filter_field_id'):
            if item.get(key) is not None and (type(item[key]) is not int or item[key] < 1):
                raise ValueError('Field and relationship references must be valid IDs.')
        for key in ('required','readonly'):
            if item.get(key) is not None and not isinstance(item[key], bool):
                raise ValueError('Required and read-only settings must be boolean.')
            item[key] = bool(item.get(key))
        for key, limit in [('label_override',160),('help_text',255),('widget',30)]:
            if item.get(key) is not None and (not isinstance(item[key], str) or len(item[key]) > limit):
                raise ValueError(f'{key} is too long or invalid.')
        if item['kind'] == 'field':
            field = fields.get(item.get('field_id'))
            if not field or field.id in seen:
                raise ValueError('Use each field at most once and only from this table.')
            names[field.phys_name] = field
            seen.add(field.id)
        elif item['kind'] == 'relation':
            rel = session.get(MetaRelation, item.get('relation_id'))
            if not rel or rel.kind != 'mn' or form.table_id not in (rel.from_table_id, rel.to_table_id):
                raise ValueError('Choose a many-to-many relationship involving this table.')
        elif not item.get('label_override'):
            raise ValueError('Give each section a heading.')
    rules_config = {}
    for item in config['items']:
        rules = form_rules.parse(item.get('rules_json'))
        if item['kind'] != 'field':
            if rules:
                raise ValueError('Conditional rules currently apply to individual scalar fields and pickers.')
            continue
        field = fields[item['field_id']]
        form_rules.validate(rules, names, field.phys_name)
        if rules and field.data_type in {'file','image','formula','autonumber'}:
            raise ValueError('Uploads and generated fields cannot have conditional rules.')
        if ('show' in rules or 'readonly' in rules) and not field.nullable and not field.default_value:
            raise ValueError(f'{field.label}: a database-required field without a default cannot be hidden or conditionally read-only.')
        item['rules_json'] = dump(rules) if rules else None
        rules_config[field.phys_name] = {'rules': rules}
    form_rules.order(rules_config)
    return check_form(session, virtual(form, config))


def get_draft(session, form, actor_id):
    draft = session.scalar(select(FormDraft).where(FormDraft.form_id == form.id))
    if not draft:
        draft = FormDraft(form_id=form.id, config_json=dump(snapshot(form)),
                          base_hash=live_hash(form), updated_by=actor_id)
        session.add(draft)
        session.commit()
    return draft


def lock_draft(session, form_id, version):
    form = session.scalar(select(MetaForm).where(MetaForm.id == form_id).with_for_update())
    draft = session.scalar(select(FormDraft).where(FormDraft.form_id == form_id).with_for_update())
    if not form or not draft or type(version) is not int or draft.version != version:
        raise ValueError('This draft changed in another session. Reload before saving.')
    return form, draft


def publish(session, form, draft, actor):
    if draft.base_hash != live_hash(form):
        raise ValueError('The live form or its table changed. Start a fresh draft and review your changes.')
    config = json.loads(draft.config_json)
    issues = validate_config(session, form, config)
    if any(i['level'] == 'error' for i in issues):
        raise ValueError('Resolve configuration errors before publishing.')
    if draft.tested_hash != digest(config):
        raise ValueError('Run a successful preview of this saved draft before publishing.')
    session.add(FormRevision(form_id=form.id, config_json=dump(snapshot(form)),
                             published_by=actor.id, description='Before publication'))
    for key in FORM_KEYS:
        setattr(form, key, config[key])
    form.items.clear()
    session.flush()
    for pos, item in enumerate(config['items']):
        form.items.append(MetaFormField(position=pos, **{key:item.get(key) for key in ITEM_KEYS}))
    session.flush()
    published = snapshot(form)
    session.add(FormRevision(form_id=form.id, config_json=dump(published),
                             published_by=actor.id, description='Published'))
    session.add(SecurityEvent(actor=f'{actor.id}:{actor.username}', event='form.published',
                              target=str(form.id), details=dump({'draft_version':draft.version})))
    draft.base_hash = live_hash(form)
    draft.config_json = dump(published)
    draft.version += 1
    draft.tested_hash = None
    session.commit()


def comparison(live, draft):
    changes = []
    for key in FORM_KEYS:
        if live.get(key) != draft.get(key):
            changes.append({'setting':key, 'before':live.get(key), 'after':draft.get(key)})
    def key(item, index):
        return f"{item['kind']}:{item.get('field_id') or item.get('relation_id') or index}"
    before = {key(i,n): {'position':n, **i} for n,i in enumerate(live['items'])}
    after = {key(i,n): {'position':n, **i} for n,i in enumerate(draft['items'])}
    for name in before.keys() | after.keys():
        if before.get(name) != after.get(name):
            changes.append({'setting':name, 'before':before.get(name), 'after':after.get(name)})
    return changes
