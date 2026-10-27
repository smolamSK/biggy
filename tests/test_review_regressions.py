"""Regression coverage for authentication, downloads and concurrent numbering."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.auth.routes import _safe_next
from app.metadata.models import Sequence
from app.record_service import _next_autonumber


@pytest.mark.parametrize('destination', [
    'https://evil.example', '//evil.example', '/\\evil.example',
    '/%2fevil.example', '/%255cevil.example', '/\nevil.example',
    'javascript:alert(1)', '', None,
])
def test_redirect_rejects_external_destinations(destination):
    application = Flask(__name__)
    application.add_url_rule('/', endpoint='core.index', view_func=lambda: '')
    with application.test_request_context():
        assert _safe_next(destination) == '/'


@pytest.mark.parametrize('destination', ['/u/', '/u/forms/1?page=2', '/portal/#ticket'])
def test_redirect_preserves_local_destinations(destination):
    with Flask(__name__).test_request_context():
        assert _safe_next(destination) == destination


def _concurrent_numbers(engine, field_id):
    barrier = Barrier(8)

    def allocate(_):
        with Session(engine) as session:
            barrier.wait(timeout=15)
            return _next_autonumber(session, SimpleNamespace(id=field_id, default_value='INV-'))

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(allocate, range(8)))


def test_autonumber_concurrent_sqlite(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path / "counter.db"}')
    Sequence.__table__.create(engine)
    try:
        assert set(_concurrent_numbers(engine, 1)) == {f'INV-{n:04d}' for n in range(1, 9)}
        assert set(_concurrent_numbers(engine, 1)) == {f'INV-{n:04d}' for n in range(9, 17)}
    finally:
        engine.dispose()


def test_autonumber_concurrent_mysql(app, client):
    from app.db import get_engine
    from tests.helpers import _setup
    _setup(client)
    with app.app_context():
        engine = get_engine()
        assert set(_concurrent_numbers(engine, 987654)) == {f'INV-{n:04d}' for n in range(1, 9)}
        assert set(_concurrent_numbers(engine, 987654)) == {f'INV-{n:04d}' for n in range(9, 17)}


def test_disabled_browser_session(app, client):
    from app.db import SessionLocal
    from app.metadata.models import AppUser
    from tests.helpers import _setup
    _setup(client)
    with client.session_transaction() as web_session:
        uid = int(web_session['_user_id'])
    with app.app_context():
        session = SessionLocal()
        session.get(AppUser, uid).is_active_flag = False
        session.commit()
    assert client.get('/auth/account').status_code == 302
    assert client.get('/api/v1/').status_code == 401


def test_portal_api_blocked(app, client):
    from tests.helpers import _mint, _setup
    from tests.test_portal import _new_portal_user
    _setup(client)
    portal = _new_portal_user(app, client, 'customer')
    token = _mint(app, 'customer')
    api = app.test_client()
    for path in ['/api/v1/', '/api/v1/openapi.json', '/api/v1/docs', '/api/v1/private']:
        assert portal.get(path).status_code == 403
        assert api.get(path, headers=token).status_code == 403
    assert portal.post('/api/v1/private', json={}).status_code == 403


@pytest.mark.parametrize('module_name', ['app.user.routes', 'app.portal.routes'])
@pytest.mark.parametrize('content_type,filename', [('text/html', 'page.html'),
                                                  ('image/svg+xml', 'image.svg')])
def test_untrusted_download_headers(monkeypatch, tmp_path, module_name, content_type, filename):
    import importlib

    routes = importlib.import_module(module_name)
    payload = b'<script>alert(1)</script>'
    path = tmp_path / filename
    path.write_bytes(payload)
    attachment = SimpleNamespace(field_id=1, row_pk=1, stored_name=filename,
                                 original_name=filename, content_type=content_type)
    from app import access
    monkeypatch.setattr(access, 'attachment', lambda *args, **kwargs: attachment)
    monkeypatch.setattr(routes, '_s', lambda: None)
    monkeypatch.setattr(routes.file_store, 'abs_path', lambda *args: str(path))
    with Flask(__name__).test_request_context():
        response = routes.attachment(1)
        response.direct_passthrough = False
        assert response.data == payload
        assert response.mimetype == 'application/octet-stream'
        assert response.headers['Content-Disposition'].startswith('attachment;')
        assert response.headers['X-Content-Type-Options'] == 'nosniff'
        response.close()


@pytest.mark.parametrize('flow', ['password', 'mfa', 'oidc'])
@pytest.mark.parametrize('destination,expected', [('https://evil.example/', '/'),
                                                 ('/u/?page=2', '/u/?page=2')])
def test_login_flow_redirects(monkeypatch, flow, destination, expected):
    from app.auth import routes
    application = Flask(__name__)
    application.secret_key = 'test'
    application.register_blueprint(routes.bp)
    application.add_url_rule('/', endpoint='core.index', view_func=lambda: '')
    user = SimpleNamespace(id=1, is_active=True, mfa_enabled=True, totp_secret='secret',
                           check_password=lambda value: True)
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(routes, 'SessionLocal', lambda: SimpleNamespace(
        scalar=lambda *args: user, get=lambda *args: user))
    monkeypatch.setattr(routes, 'establish_session', lambda user: None)
    monkeypatch.setattr(routes, '_login_limits', lambda: (0, 300))
    monkeypatch.setattr(routes.rate_limit, 'blocked', lambda *args: (False, 0))
    form = SimpleNamespace(validate_on_submit=lambda: True,
                           username=SimpleNamespace(data='user'),
                           password=SimpleNamespace(data='password'),
                           code=SimpleNamespace(data='123456'))
    monkeypatch.setattr(routes, 'LoginForm', lambda: form)
    monkeypatch.setattr(routes, 'MfaCodeForm', lambda: form)
    client = application.test_client()
    if flow == 'password':
        user.mfa_enabled = False
        response = client.post('/auth/login', query_string={'next': destination})
    elif flow == 'mfa':
        client.post('/auth/login', query_string={'next': destination})
        with client.session_transaction() as session:
            assert session['_mfa_next'] == expected
            session['_mfa_next'] = destination  # old sessions are revalidated too
        monkeypatch.setattr(routes.totp, 'verify', lambda *args: True)
        response = client.post('/auth/mfa-verify')
    else:
        monkeypatch.setattr(routes.oidc, 'enabled', lambda: True)
        monkeypatch.setattr(routes.oidc, 'authorize_url', lambda *args: 'https://idp.example/')
        monkeypatch.setattr(routes.oidc, 'exchange_code', lambda *args: {'id_token': 'token'})
        monkeypatch.setattr(routes.oidc, 'verify_id_token', lambda *args: {})
        monkeypatch.setattr(routes, '_map_oidc_user', lambda *args: user)
        client.get('/auth/oidc/login', query_string={'next': destination})
        with client.session_transaction() as session:
            assert session['_oidc_next'] == expected
            session['_oidc_next'] = destination
            state = session['_oidc_state']
        response = client.get('/auth/oidc/callback', query_string={'state': state, 'code': 'code'})
    assert response.status_code == 302
    assert response.headers['Location'] == expected
