"""Authorization, isolated capabilities, audit durability and canonical API coverage."""
import time
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import access, security_audit
from app.metadata.models import AppUser, Base, MetaFieldPermission, SecurityEvent
from app.previews import _signer, create_preview_app, preview_url


@pytest.fixture
def preview_environment(tmp_path):
    folder = tmp_path / '1'
    folder.mkdir()
    path = folder / 'test.png'
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + b'test')
    config = {'UPLOAD_FOLDER': str(tmp_path), 'PREVIEW_SIGNING_KEY': 'test-key',
              'PREVIEW_BASE_URL': 'https://preview.example.net'}
    app = Flask(__name__)
    app.config.update(config)
    att = SimpleNamespace(field_id=1, stored_name='test.png', original_name='test.png')
    with app.test_request_context(base_url='https://app.example.com'):
        url = preview_url(att, path)
    return app, create_preview_app(config).test_client(), att, path, url


def test_preview_capability(preview_environment, monkeypatch):
    app, client, att, path, url = preview_environment
    route = '/preview/' + url.rsplit('/', 1)[-1]
    response = client.get(route, headers={'Cookie': 'session=untrusted'})
    assert response.status_code == 200
    assert response.mimetype == 'image/png'
    assert response.headers['Content-Disposition'].startswith('inline;')
    assert 'Set-Cookie' not in response.headers
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'sandbox' in response.headers['Content-Security-Policy']
    assert client.get(route + 'tampered').status_code == 404
    now = time.time()
    monkeypatch.setattr('itsdangerous.timed.time.time', lambda: now + 61)
    assert client.get(route).status_code == 404


def test_preview_rejects_unsafe_origins_and_formats(preview_environment):
    app, client, att, path, url = preview_environment
    with app.test_request_context(base_url='https://app.example.com'):
        for origin in ('https://app.example.com', 'http://preview.example.net',
                       'https://preview.example.net/extra'):
            app.config['PREVIEW_BASE_URL'] = origin
            assert preview_url(att, path) is None
        app.config.update(PREVIEW_BASE_URL='https://preview.example.net',
                          SESSION_COOKIE_DOMAIN='example.net')
        assert preview_url(att, path) is None
        app.config['SESSION_COOKIE_DOMAIN'] = None
        path.write_bytes(b'<svg onload="alert(1)"></svg>')
        assert preview_url(att, path) is None
    assert client.get('/preview/' + url.rsplit('/', 1)[-1]).status_code == 404
    malicious = _signer(app.config).dumps({'field': 1, 'file': '../secret',
                                         'mime': 'image/png', 'name': 'secret'})
    assert client.get('/preview/' + malicious).status_code == 404


def test_security_audit_transaction_and_secrets():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    security_audit.install(Flask(__name__))
    with Session(engine) as session:
        user = AppUser(username='alice', password_hash='NEVER-LOG-THIS', role='user',
                       is_active_flag=True)
        session.add(user)
        session.commit()
        initial = len(session.scalars(select(SecurityEvent)).all())
        user.is_active_flag = False
        session.flush()
        session.rollback()
        assert len(session.scalars(select(SecurityEvent)).all()) == initial
        user.is_active_flag = False
        session.commit()
        entries = session.scalars(select(SecurityEvent)).all()
        assert entries[-1].event == 'account.changed'
        assert 'is_active_flag' in entries[-1].details
        assert all('NEVER-LOG-THIS' not in e.details for e in entries)
    engine.dispose()


def test_shared_policy_denies_portal_and_hidden_fields():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    field = SimpleNamespace(id=1, phys_name='secret')
    table = SimpleNamespace(id=1, fields=[field], soft_delete=False, row_owned=True)
    user = SimpleNamespace(id=2, role='user', is_authenticated=True, is_designer=False,
                           is_portal=False)
    with Session(engine) as session:
        session.add(MetaFieldPermission(role='user', field_id=1, access='none'))
        session.commit()
        assert access.readable_fields(session, user, table) == set()
        assert not access.can_read(access.field_access(session, user, field))
        assert not access.row_readable(table, {'created_by': None}, user_id=2, is_designer=False)
        assert access.row_readable(table, {'created_by': 2}, user_id=2, is_designer=False)
        user.is_portal = True
        assert not access.table_readable(session, user, table)
    engine.dispose()


def test_reserved_natural_keys(app, client):
    from app.db import SessionLocal
    from app.metadata.models import MetaTable
    from tests.helpers import _make_form, _ok, _setup
    _setup(client)
    _ok(client.post('/designer/tables/new', data={
        'phys_name': 'codes', 'label': 'Codes', 'source_id': '0', 'pk_mode': 'custom',
        'pk_name': 'code', 'pk_type': 'string', 'pk_length': '30'}, follow_redirects=True))
    with app.app_context():
        tid = SessionLocal().scalar(select(MetaTable).where(MetaTable.phys_name == 'codes')).id
    _make_form(client, app, 'codes_form', 'Codes', tid)
    for key in ('fields', 'bulk', '123'):
        response = client.post('/api/v1/tables/codes/records', json={'code': key})
        assert response.status_code == 201, response.data
        location = response.headers['Location']
        assert location == f'/api/v1/tables/codes/records/{key}'
        assert client.get(location).get_json()['code'] == key
        assert client.patch(location, json={}).status_code == 200
        assert client.delete(location).status_code == 204
    assert client.get('/api/v1/tables/codes/fields').status_code == 200
    assert client.get('/designer/security-history?q=account').status_code == 200


def test_authorized_preview_and_security_history(app, client, monkeypatch):
    import io

    from app.db import SessionLocal
    from app.metadata.models import ApiToken, Attachment
    from tests.helpers import _mint, _new_amy, _ok, _setup
    from tests.test_crud_ui import _doc_with_files
    _setup(client)
    tid, fid, fields = _doc_with_files(client, app)
    _ok(client.post(f'/u/forms/{fid}/new', data={
        'title': 'Document', f'file_{fields["photo"]}':
            (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'bytes'), 'test.png')},
        content_type='multipart/form-data', follow_redirects=True))
    with app.app_context():
        att_id = SessionLocal().scalar(select(Attachment.id))
    monkeypatch.setitem(app.config, 'PREVIEW_BASE_URL', 'https://preview.example.net')
    monkeypatch.setitem(app.config, 'PREVIEW_SIGNING_KEY', 'test-preview-key')
    response = client.get(f'/u/attachment/{att_id}?preview=1')
    assert response.status_code == 302
    preview = create_preview_app(app.config).test_client()
    token_path = '/preview/' + response.headers['Location'].rsplit('/', 1)[-1]
    assert preview.get(token_path).status_code == 200
    amy = _new_amy(app, client)
    with app.app_context():
        session = SessionLocal()
        session.add(MetaFieldPermission(role='user', field_id=fields['photo'], access='none'))
        session.commit()
    assert amy.get(f'/u/attachment/{att_id}?preview=1').status_code == 403
    assert amy.get('/designer/security-history').status_code == 403
    _mint(app, 'boss', 'audit-test')
    with app.app_context():
        token_id = SessionLocal().scalar(select(ApiToken.id).where(ApiToken.name == 'audit-test'))
    client.post(f'/u/tokens/{token_id}/revoke')
    with app.app_context():
        events = SessionLocal().scalars(select(SecurityEvent)).all()
        kinds = {e.event for e in events}
        assert {'token.created', 'token.changed', 'access.denied', 'permission.field.created'} <= kinds
        assert all('token_hash' not in e.details and 'test-preview-key' not in e.details for e in events)
    page = client.get('/designer/security-history?q=token.changed')
    assert page.status_code == 200
    assert b'token.changed' in page.data
    assert b'permission.field.created' not in page.data
