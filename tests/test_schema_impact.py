"""Read-only impact reports, scoped dependencies and data-source failures."""
import json

from sqlalchemy import func, select, text

from app.db import SessionLocal, get_engine
from app.metadata.models import (
    FormDraft,
    MetaField,
    MetaForm,
    MetaFormField,
    MetaRelation,
    MetaTable,
)
from tests.helpers import _add_field, _make_form, _make_table, _new_amy, _ok, _setup


def test_field_dependencies_and_counts_are_read_only(app, client):
    _setup(client)
    tid = _make_table(client, app, 'impact_note', 'Notes', 'body')
    _add_field(client, tid, 'detail', 'string', length=80)
    fid = _make_form(client, app, 'impact_notes', 'Notes form', tid)
    with app.app_context():
        session = SessionLocal()
        form = session.get(MetaForm, fid)
        fields = {f.phys_name:f for f in form.table.fields}
        field_id = fields['body'].id
        detail = next(i for i in form.items if i.field_id == fields['detail'].id)
        detail.rules_json = json.dumps({'show':{'field':'body','op':'eq','value':'yes'}})
        session.commit()
        with get_engine().begin() as conn:
            conn.execute(text("INSERT INTO impact_note (body) VALUES ('yes'),(NULL)"))
    _ok(client.get(f'/designer/forms/{fid}/builder'))
    path = f'/designer/tables/{tid}/fields/{field_id}/impact'
    report = client.get(path)
    _ok(report)
    assert 'Notes form' in report.text
    assert 'Uses this field in a conditional rule' in report.text
    assert 'Live forms' in report.text and 'Saved drafts' in report.text
    assert 'Count existing records' in report.text
    counted = client.get(path+'?counts=1')
    _ok(counted)
    assert '<dd>2</dd>' in counted.text and '<dd>1</dd>' in counted.text
    with app.app_context():
        session = SessionLocal()
        assert session.scalar(select(func.count()).select_from(FormDraft)) == 1
        assert session.scalar(select(func.count()).select_from(MetaFormField)) == 2
        with get_engine().connect() as conn:
            assert conn.execute(text('SELECT COUNT(*) FROM impact_note')).scalar_one() == 2
    # No draft is created just by viewing impact.
    another_fid = _make_form(client, app, 'impact_other', 'Other form', tid)
    _ok(client.get(f'/designer/tables/{tid}/impact'))
    with app.app_context():
        assert SessionLocal().scalar(select(FormDraft).where(FormDraft.form_id == another_fid)) is None
    amy = _new_amy(app, client)
    assert amy.get(path+'?counts=1').status_code == 403
    other_tid = _make_table(client, app, 'other_impact', 'Unrelated', 'name')
    assert client.get(f'/designer/tables/{other_tid}/fields/{field_id}/impact').status_code == 404
    assert client.get('/designer/relations/999999/impact').status_code == 404


def test_relation_impact_and_dependent_match_field(app, client):
    _setup(client)
    parent = _make_table(client, app, 'impact_parent', 'Parent', 'name')
    child = _make_table(client, app, 'impact_child', 'Child', 'name')
    job = _make_table(client, app, 'impact_job', 'Job', 'name')
    for source, target, column in [(child,parent,'parent_id'), (job,parent,'parent_id'), (job,child,'child_id')]:
        _ok(client.post('/designer/relations/new-m1', data={
            'name':column+str(source),'from_table_id':source,'to_table_id':target,
            'field_name':column,'on_delete':'SET NULL','nullable':'y'}, follow_redirects=True))
    fid = _make_form(client, app, 'impact_job_form', 'Dependent job form', job)
    with app.app_context():
        session = SessionLocal()
        match = session.scalar(select(MetaField).where(MetaField.table_id == child, MetaField.phys_name == 'parent_id'))
        match_id = match.id
        relation = session.scalar(select(MetaRelation).where(MetaRelation.from_field_id == match.id))
        relation_id = relation.id
        form = session.get(MetaForm, fid)
        fields = {f.phys_name:f.id for f in form.table.fields}
        item = next(i for i in form.items if i.field_id == fields['child_id'])
        item.parent_field_id = fields['parent_id']
        item.filter_field_id = match_id
        session.commit()
    parent_report = client.get(f'/designer/tables/{parent}/impact')
    _ok(parent_report)
    assert 'Dependent job form' in parent_report.text
    assert 'Selects related records from this table' in parent_report.text
    report = client.get(f'/designer/tables/{child}/fields/{match_id}/impact')
    _ok(report)
    assert 'Dependent job form' in report.text
    assert 'Uses this field to filter a dependent dropdown' in report.text
    _ok(client.get(f'/designer/relations/{relation_id}/impact?counts=1'))
    _ok(client.post('/designer/relations/new-mn', data={
        'name':'Many links','from_table_id':parent,'to_table_id':job}, follow_redirects=True))
    with app.app_context():
        rel_id = SessionLocal().scalar(select(MetaRelation.id).where(MetaRelation.name == 'Many links'))
    report = client.get(f'/designer/relations/{rel_id}/impact?counts=1')
    _ok(report)
    assert 'Saved relationship links' in report.text and '<dd>0</dd>' in report.text


def test_unavailable_counts_keep_dependencies_visible(app, client, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from app import schema_impact
    _setup(client)
    tid = _make_table(client, app, 'impact_unavailable', 'Unavailable', 'body')
    _make_form(client, app, 'unavailable_form', 'Still visible', tid)

    def unavailable(*args):
        raise OperationalError('private connection details', {}, Exception('secret'))

    monkeypatch.setattr(schema_impact.data_service, 'reflect_table', unavailable)
    report = client.get(f'/designer/tables/{tid}/impact?counts=1')
    _ok(report)
    assert 'Record counts are unavailable' in report.text
    assert 'Still visible' in report.text
    assert 'private connection details' not in report.text and 'secret' not in report.text


def test_external_table_impact_and_escaped_labels(app, client):
    _setup(client)
    tid = _make_table(client, app, 'impact_external', 'External', 'body')
    with app.app_context():
        table = SessionLocal().get(MetaTable, tid)
        table.managed = False
        table.label = '<script>alert(1)</script>'
        SessionLocal().commit()
    report = client.get(f'/designer/tables/{tid}/impact')
    _ok(report)
    assert 'physical table and records remain' in report.text
    assert '<script>alert(1)</script>' not in report.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in report.text


def test_saved_report_links_and_unreadable_drafts(app, client):
    from app.metadata.models import AppUser, ReportDef
    _setup(client)
    tid = _make_table(client, app, 'impact_report', 'Reports', 'body')
    fid = _make_form(client, app, 'impact_report_form', 'Draft to inspect', tid)
    _ok(client.get(f'/designer/forms/{fid}/builder'))
    with app.app_context():
        session = SessionLocal()
        uid = session.scalar(select(AppUser.id).where(AppUser.username == 'boss'))
        session.add(ReportDef(table_id=tid, user_id=uid, name='Grouped report',
                              query='group=body&export=csv'))
        session.scalar(select(FormDraft).where(FormDraft.form_id == fid)).config_json = '{invalid'
        session.commit()
    report = client.get(f'/designer/tables/{tid}/impact')
    _ok(report)
    assert f'/u/report/{tid}?group=body' in report.text
    assert 'export=csv' not in report.text
    assert 'Draft for Draft to inspect could not be inspected.' in report.text
