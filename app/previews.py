"""Isolated, session-free preview server and short-lived signed capabilities."""
import os
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, abort, current_app, request, send_file
from itsdangerous import BadData, URLSafeTimedSerializer

TTL = 60


def _signer(config):
    secret = config.get('PREVIEW_SIGNING_KEY')
    if not secret:
        raise ValueError('PREVIEW_SIGNING_KEY is required')
    return URLSafeTimedSerializer(secret, salt='attachment-preview-v1')


def preview_type(path):
    with open(path, 'rb') as stream:
        head = stream.read(16)
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return 'image/webp'
    if head.startswith(b'%PDF-'):
        return 'application/pdf'
    return None


def preview_url(att, path):
    config = current_app.config
    base = config.get('PREVIEW_BASE_URL', '').rstrip('/')
    if not base or not config.get('PREVIEW_SIGNING_KEY'):
        return None
    origin = urlsplit(base)
    app_host = request.host.split(':')[0].lower()
    cookie_domain = (config.get('SESSION_COOKIE_DOMAIN') or app_host).lstrip('.').lower()
    host = (origin.hostname or '').lower()
    if (origin.scheme != 'https' or not host or origin.username or origin.password
            or origin.path or origin.query or origin.fragment or host == app_host
            or host == cookie_domain or host.endswith('.' + cookie_domain)):
        return None
    mime = preview_type(path)
    if not mime:
        return None
    token = _signer(config).dumps({'field': att.field_id, 'file': att.stored_name,
                                  'name': att.original_name, 'mime': mime})
    return f'{base}/preview/{token}'


def create_preview_app(config=None):
    """Deploy separately with read-only uploads; no DB, login or session middleware."""
    app = Flask(__name__, static_folder=None)
    app.config.update(UPLOAD_FOLDER=os.environ.get('UPLOAD_FOLDER', 'instance/uploads'),
                      PREVIEW_SIGNING_KEY=os.environ.get('PREVIEW_SIGNING_KEY'))
    if config:
        app.config.update(config)
    signer = _signer(app.config)

    @app.get('/preview/<token>')
    def preview(token):
        try:
            payload = signer.loads(token, max_age=TTL)
            field, filename = int(payload['field']), payload['file']
            if field < 1 or not isinstance(filename, str) or Path(filename).name != filename:
                abort(404)
            root = Path(app.config['UPLOAD_FOLDER']).resolve()
            path = (root / str(field) / filename).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                abort(404)
            mime = preview_type(path)
            if not mime or mime != payload['mime']:
                abort(404)
        except (BadData, KeyError, TypeError, ValueError, OSError):
            abort(404)
        return send_file(path, mimetype=mime, download_name=payload['name'],
                         as_attachment=False, conditional=False)

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy'] = "sandbox; default-src 'none'; frame-ancestors 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers.pop('Set-Cookie', None)
        return response

    return app
