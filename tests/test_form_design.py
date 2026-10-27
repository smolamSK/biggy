"""Draft isolation, publication gates and server-side conditional behavior."""
import io
import json
import re

import pytest
from sqlalchemy import select, text

from app import form_rules
from app.db import SessionLocal, get_engine
from app.metadata.models import MetaForm
from tests.helpers import _add_field, _make_form, _make_table, _new_amy, _ok, _setup


def studio(client, fid):
    response = client.get(f'/designer/forms/{fid}/builder')
    _ok(response)
    return json.loads(re.search(r'id="studio-data" type="application/json">(.*?)</script>',
                               response.text, re.S)[1])


def test_draft_publish_restore_and_conflicts(app, client):
    _setup(client)
    tid = _make_table(client, app, 'draft_note', 'Notes', 'body')
    fid = _make_form(client, app, 'draft_form', 'Original', tid)
    _ok(client.get('/designer/setup-guide'))
    data = studio(client, fid)
    base = f'/designer/forms/{fid}/builder/'
    data['config']['title'] = 'Published title'
    saved = client.post(base+'save', json={'version':data['version'], 'config':data['config']})
    _ok(saved)
    state = saved.json
    with app.app_context():
        assert SessionLocal().get(MetaForm, fid).title == 'Original'
    assert client.post(base+'save', json={'version':data['version'], 'config':data['config']}).status_code == 409
    assert client.post(base+'publish', json={'version':state['version']}).status_code == 400
    tested = client.post(base+'preview', json={'version':state['version'], 'role':'user', 'sample':{'body':'Hello'}})
    _ok(tested)
    assert tested.json['tested']
    published = client.post(base+'publish', json={'version':tested.json['version']})
    _ok(published)
    assert published.json['config']['title'] == 'Published title'
    revision = published.json['revisions'][-1]['id']
    restored = client.post(base+'restore', json={'version':published.json['version'], 'revision_id':revision})
    _ok(restored)
    assert restored.json['config']['title'] == 'Original'
    with app.app_context():
        assert SessionLocal().get(MetaForm, fid).title == 'Published title'
    _ok(client.post(f'/designer/forms/{fid}/catalog', data={'catalog_group':'Other editor'}))
    assert client.post(base+'publish', json={'version':restored.json['version']}).status_code == 409
    amy = _new_amy(app, client)
    assert amy.get(f'/designer/forms/{fid}/builder').status_code == 403


def test_rules_require_and_preserve_hidden_values(app, client):
    _setup(client)
    tid = _make_table(client, app, 'conditional_note', 'Notes', 'body')
    _add_field(client, tid, 'detail', 'string', length=80)
    fid = _make_form(client, app, 'conditional_form', 'Conditional', tid)
    data = studio(client, fid)
    detail = next(i for i in data['config']['items'] if i['field_id'] == next(f['id'] for f in data['fields'] if f['name']=='detail'))
    condition = {'field':'body', 'op':'eq', 'value':'yes'}
    detail['rules_json'] = json.dumps({'show':condition, 'require':condition})
    base = f'/designer/forms/{fid}/builder/'
    saved = client.post(base+'save', json={'version':data['version'], 'config':data['config']})
    _ok(saved)
    tested = client.post(base+'preview', json={'version':saved.json['version'], 'role':'user', 'sample':{'body':'no'}})
    _ok(tested)
    _ok(client.post(base+'publish', json={'version':tested.json['version']}))
    response = client.post(f'/u/forms/{fid}/new', data={'body':'yes','detail':''})
    assert response.status_code == 200 and 'required' in response.text.lower()
    _ok(client.post(f'/u/forms/{fid}/new', data={'body':'yes','detail':'keep'}))
    with app.app_context():
        with get_engine().connect() as conn:
            pk = conn.execute(text('SELECT id FROM conditional_note')).scalar_one()
    _ok(client.post(f'/u/forms/{fid}/{pk}/edit', data={'body':'no','detail':'tampered'}))
    with app.app_context():
        with get_engine().connect() as conn:
            assert conn.execute(text('SELECT detail FROM conditional_note')).scalar_one() == 'keep'
    assert client.post(f'/u/forms/{fid}/new', data={'body':'no','detail':'tampered'}).status_code == 302
    with app.app_context():
        with get_engine().connect() as conn:
            assert conn.execute(text('SELECT detail FROM conditional_note ORDER BY id DESC')).scalar() is None

    exported = client.get('/designer/schema/export.json')
    _ok(exported)
    _ok(client.post('/designer/schema/import',
                    data={'file':(io.BytesIO(exported.data), 'schema.json'), 'replace_existing':'y'},
                    content_type='multipart/form-data'))
    with app.app_context():
        restored = SessionLocal().scalar(select(MetaForm).where(MetaForm.name == 'conditional_form'))
        assert any(i.rules_json and json.loads(i.rules_json).get('require') == condition for i in restored.items)


def test_rules_cycles_and_untrusted_controllers():
    def rule(name):
        return {'show':{'field':name,'op':'eq','value':'yes'}}
    with pytest.raises(ValueError, match='cycle'):
        form_rules.order({'a':{'rules':rule('b')},'b':{'rules':rule('a')}})
    config = {'a':{'type':'string','locked':True}, 'b':{'type':'string','rules':rule('a')}}
    assert not form_rules.states(config, {'a':'yes'}, {'a':'no'})['b']['visible']
    assert not form_rules.matches({'field':'secret','op':'empty'}, {})
    assert form_rules.matches({'field':'n','op':'gt','value':'9007199254740992'},
                              {'n':'9007199254740993'}, {'n':'bigint'})


def test_preview_honors_form_and_field_permissions(app, client):
    from app.metadata.models import MetaFieldPermission, MetaPermission
    _setup(client)
    tid = _make_table(client, app, 'preview_permission', 'Preview', 'body')
    fid = _make_form(client, app, 'preview_permission_form', 'Preview', tid)
    state = studio(client, fid)
    with app.app_context():
        session = SessionLocal()
        session.add(MetaPermission(form_id=fid, role='user', access='read'))
        session.commit()
    base = f'/designer/forms/{fid}/builder/'
    result = client.post(base+'preview', json={'version':state['version'], 'role':'user'})
    _ok(result)
    assert all(f['readonly'] for f in result.json['preview']['fields'])
    with app.app_context():
        session = SessionLocal()
        session.add(MetaFieldPermission(field_id=state['fields'][0]['id'], role='user', access='none'))
        session.commit()
    hidden = client.post(base+'preview', json={'version':result.json['version'], 'role':'user'})
    _ok(hidden)
    assert not hidden.json['preview']['fields'][0]['visible']
    assert hidden.json['preview']['fields'][0]['reason'] == 'Hidden by field permissions'
    with app.app_context():
        session = SessionLocal()
        session.scalar(select(MetaPermission).where(MetaPermission.form_id == fid)).access = 'none'
        session.commit()
    denied = client.post(base+'preview', json={'version':hidden.json['version'], 'role':'user'})
    _ok(denied)
    assert not denied.json['preview']['allowed']
    assert denied.json['preview']['fields'] == []
    assert not denied.json['tested']
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        assert client.post(base+'reset', json={'version':denied.json['version']}).status_code == 400
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


@pytest.mark.parametrize('rules', [
    {'show':{'field':'missing','op':'eq','value':'yes'}},
    {'show':{'field':'detail','op':'eq','value':'yes'}},
    {'show':{'all':[]}},
    {'show':{'field':'body','op':'gt','value':'1'}},
])
def test_invalid_rules_rejected(rules):
    from types import SimpleNamespace
    fields = {'body':SimpleNamespace(data_type='string'), 'detail':SimpleNamespace(data_type='string')}
    with pytest.raises(ValueError):
        form_rules.validate(rules, fields, 'detail')


def test_readonly_default_controls_rules_on_create(app, client):
    _setup(client)
    tid = _make_table(client, app, 'default_rule', 'Defaults', 'body')
    _add_field(client, tid, 'category', 'string', length=80, default_value='yes')
    fid = _make_form(client, app, 'default_rule_form', 'Defaults', tid)
    state = studio(client, fid)
    fields = {f['id']:f['name'] for f in state['fields']}
    for item in state['config']['items']:
        if fields[item['field_id']] == 'category':
            item['readonly'] = True
        else:
            item['rules_json'] = json.dumps({'require':{'field':'category','op':'eq','value':'yes'}})
    base = f'/designer/forms/{fid}/builder/'
    saved = client.post(base+'save', json={'version':state['version'], 'config':state['config']})
    _ok(saved)
    tested = client.post(base+'preview', json={'version':saved.json['version'], 'role':'user', 'sample':{'body':'Test'}})
    _ok(tested)
    _ok(client.post(base+'publish', json={'version':tested.json['version']}))
    rejected = client.post(f'/u/forms/{fid}/new', data={'body':'','category':'no'})
    assert rejected.status_code == 200 and 'required' in rejected.text.lower()
    accepted = client.post(f'/u/forms/{fid}/new', data={'body':'Test','category':'no'})
    assert accepted.status_code == 302
    with app.app_context():
        with get_engine().connect() as conn:
            assert conn.execute(text('SELECT category FROM default_rule')).scalar_one() == 'yes'


def test_schema_constraints_invalidate_draft():
    from app.form_design import live_hash
    from app.metadata.models import MetaField, MetaTable
    table = MetaTable(phys_name='sample', label='Sample')
    table.fields.append(MetaField(phys_name='body', label='Body', data_type='string'))
    form = MetaForm(table=table, name='sample', title='Sample')
    original = live_hash(form)
    table.fields[0].max_length = 20
    assert live_hash(form) != original
