"""Reusable layouts remap schema references without publishing or granting access."""
import json
import re

import pytest
from sqlalchemy import func, select

from app import form_design, form_templates
from app.db import SessionLocal
from app.metadata.models import (
    FormDraft,
    FormTemplate,
    MetaField,
    MetaForm,
    MetaPermission,
    MetaRelation,
)
from tests.helpers import _add_field, _make_form, _make_table, _new_amy, _ok, _setup


def save_template(client, fid, name='Reusable', source='live', version=''):
    page = client.get(f'/designer/form-templates/save/{fid}')
    _ok(page)
    live_hash = re.search(r'name="live_hash" value="([a-f0-9]+)"', page.text)[1]
    return client.post(f'/designer/form-templates/save/{fid}', data={
        'name':name, 'description':'Reusable conditions', 'source':source,
        'live_hash':live_hash, 'version':version}, follow_redirects=True)


def fixture_forms(client, app):
    _setup(client)
    source_tid = _make_table(client, app, 'template_source', 'Source', 'choice')
    _add_field(client, source_tid, 'detail', 'string', length=80)
    source = _make_form(client, app, 'template_source_form', 'Source form', source_tid)
    target_tid = _make_table(client, app, 'template_target', 'Target', 'answer')
    _add_field(client, target_tid, 'explanation', 'string', length=80)
    target = _make_form(client, app, 'template_target_form', 'Target form', target_tid)
    with app.app_context():
        session = SessionLocal()
        form = session.get(MetaForm, source)
        detail = next(i for i in form.items if session.get(MetaField, i.field_id).phys_name == 'detail')
        detail.rules_json = json.dumps({'require':{'all':[
            {'field':'choice','op':'eq','value':'yes'},
            {'any':[{'field':'choice','op':'not_empty'}]}]}})
        session.commit()
        form = session.get(MetaForm, target)
        fields = {f.phys_name:f.id for f in form.table.fields}
    return source, target, {'fields':{'choice':fields['answer'],'detail':fields['explanation']}, 'relations':{},'filters':{}}


def test_template_remaps_conditions_and_leaves_live_untouched(app, client):
    source, target, body = fixture_forms(client, app)
    _ok(save_template(client, source))
    with app.app_context():
        session = SessionLocal()
        template_id = session.scalar(select(FormTemplate.id))
        original = form_design.snapshot(session.get(MetaForm, target))
        template_config = json.loads(session.get(FormTemplate, template_id).config_json)
        assert 'field_id' not in form_design.dump(template_config)
    key = f'saved-{template_id}'
    _ok(client.get(f'/designer/form-templates/{key}/setup?form_id={target}'))
    review = client.post(f'/designer/form-templates/{key}/review/{target}', json=body)
    _ok(review)
    assert '"field":"answer"' in review.json['config']['items'][1]['rules_json']
    with app.app_context():
        assert SessionLocal().scalar(select(func.count()).select_from(FormDraft)) == 0
    applied = client.post(f'/designer/form-templates/{key}/apply/{target}', json={**body,'token':review.json['token']})
    _ok(applied)
    with app.app_context():
        session = SessionLocal()
        assert form_design.snapshot(session.get(MetaForm, target)) == original
        draft = session.scalar(select(FormDraft).where(FormDraft.form_id == target))
        assert not draft.tested_hash
        assert json.loads(draft.config_json)['title'] == 'Target form'
        assert session.scalar(select(func.count()).select_from(MetaPermission)) == 0
    # Copy is independent of both source and template lifetime.
    _ok(client.post(f'/designer/forms/{source}/delete'))
    _ok(client.get(f'/designer/form-templates/{key}/setup?form_id={target}'))
    _ok(client.post(f'/designer/form-templates/{template_id}/delete'))
    with app.app_context():
        assert SessionLocal().scalar(select(FormDraft).where(FormDraft.form_id == target)) is not None


def test_review_token_rejects_changes_and_draft_overwrite(app, client):
    source, target, body = fixture_forms(client, app)
    _ok(save_template(client, source))
    with app.app_context():
        template_id = SessionLocal().scalar(select(FormTemplate.id))
    base = f'/designer/form-templates/saved-{template_id}'
    assert client.post(f'{base}/apply/{target}', json=body).status_code == 400
    reviewed = client.post(f'{base}/review/{target}', json=body).json
    with app.app_context():
        session = SessionLocal()
        session.add(MetaPermission(form_id=target, role='user', access='read'))
        session.commit()
    assert client.post(f'{base}/apply/{target}', json={**body,'token':reviewed['token']}).status_code == 409
    reviewed = client.post(f'{base}/review/{target}', json=body).json
    _ok(client.post(f'{base}/apply/{target}', json={**body,'token':reviewed['token']}))
    assert client.post(f'{base}/apply/{target}', json={**body,'token':reviewed['token']}).status_code == 409
    reviewed = client.post(f'{base}/review/{target}', json=body).json
    assert reviewed['replace_draft']
    assert client.post(f'{base}/apply/{target}', json={**body,'token':reviewed['token']}).status_code == 400
    _ok(client.post(f'{base}/apply/{target}', json={**body,'token':reviewed['token'],'replace_draft':True}))
    wrong = {**body, 'fields':{'choice':body['fields']['choice'],'detail':body['fields']['choice']}}
    assert client.post(f'{base}/review/{target}', json=wrong).status_code == 400
    with app.app_context():
        other = SessionLocal().get(MetaForm, source).items[0].field_id
    wrong['fields']['detail'] = other
    assert client.post(f'{base}/review/{target}', json=wrong).status_code == 400


def test_template_authorization_csrf_and_saved_draft_version(app, client):
    source, target, body = fixture_forms(client, app)
    _ok(client.get(f'/designer/forms/{source}/builder'))
    assert save_template(client, source, source='draft', version='999').status_code == 400
    _ok(save_template(client, source, source='draft', version='1'))
    amy = _new_amy(app, client)
    for path in ['/designer/form-templates',f'/designer/form-templates/save/{source}',
                 f'/designer/form-templates/service-request/setup?form_id={target}']:
        assert amy.get(path).status_code == 403
    assert amy.post(f'/designer/form-templates/service-request/apply/{target}',json=body).status_code == 403
    app.config['WTF_CSRF_ENABLED'] = True
    try:
        assert client.post(f'/designer/form-templates/service-request/review/{target}',json=body).status_code == 400
        assert client.post('/designer/form-templates/1/delete').status_code == 400
    finally:
        app.config['WTF_CSRF_ENABLED'] = False


def test_dependent_and_many_to_many_relationship_mapping(app, client):
    _setup(client)
    parent = _make_table(client, app, 'template_parent', 'Parent', 'name')
    child = _make_table(client, app, 'template_child', 'Child', 'name')
    job = _make_table(client, app, 'template_job', 'Job', 'name')
    for source, target, column in [(child,parent,'parent_id'),(job,parent,'parent_id'),(job,child,'child_id')]:
        _ok(client.post('/designer/relations/new-m1',data={'name':column+str(source),
            'from_table_id':source,'to_table_id':target,'field_name':column,'on_delete':'SET NULL','nullable':'y'}))
    _ok(client.post('/designer/relations/new-mn',data={'name':'Job parents','from_table_id':job,'to_table_id':parent}))
    fid = _make_form(client, app, 'template_job_form', 'Job form', job)
    with app.app_context():
        session = SessionLocal()
        relation = session.scalar(select(MetaRelation).where(MetaRelation.kind == 'mn'))
        relation_id = relation.id
    _ok(client.post(f'/designer/forms/{fid}',data={'kind':'relation','relation_id':relation_id}))
    with app.app_context():
        session = SessionLocal()
        form = session.get(MetaForm, fid)
        fields = {f.phys_name:f.id for f in form.table.fields}
        item = next(i for i in form.items if i.field_id == fields['child_id'])
        item.parent_field_id = fields['parent_id']
        item.filter_field_id = session.scalar(select(MetaField.id).where(MetaField.table_id == child,MetaField.phys_name == 'parent_id'))
        session.commit()
        template = form_templates.capture(session, form, form_design.snapshot(form))
        config, issues = form_templates.map_config(session, form, template, fields, {'relation_0':relation_id}, {})
        assert not issues
        mapped = next(i for i in config['items'] if i['field_id'] == fields['child_id'])
        assert mapped['parent_field_id'] == fields['parent_id']
        assert mapped['filter_field_id'] == item.filter_field_id
        assert any(i['relation_id'] == relation_id for i in config['items'])
        with pytest.raises(ValueError,match='match field'):
            form_templates.map_config(session, form, template, fields, {'relation_0':relation_id}, {'child_id':fields['name']})


@pytest.mark.parametrize('key', ['service-request','assets','approval-request'])
def test_starter_maps_to_compatible_table(app, client, key):
    _setup(client)
    template = form_templates.BUILTINS[key]['config']
    first = next(iter(template['fields']))
    tid = _make_table(client, app, 'starter_table', 'Starter', first)
    for name, spec in template['fields'].items():
        if name == first:
            continue
        _add_field(client, tid, name, spec['type'], enum_options='\n'.join(spec['choices']))
    fid = _make_form(client, app, 'starter_form', 'Starter', tid)
    with app.app_context():
        form = SessionLocal().get(MetaForm, fid)
        mapping = {f.phys_name:f.id for f in form.table.fields}
    response = client.post(f'/designer/form-templates/{key}/review/{fid}',json={'fields':mapping,'relations':{}})
    _ok(response)
    assert not response.json['issues']
    assert any(i['rules_json'] for i in response.json['config']['items'])
