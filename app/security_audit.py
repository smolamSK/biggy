"""Transactional security change history and independent denied-request logging."""
import json
import logging

from flask import has_request_context, request
from flask_login import current_user
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from .metadata.models import ApiToken, AppUser, MetaFieldPermission, MetaPermission, SecurityEvent


def _actor():
    if has_request_context() and current_user.is_authenticated:
        return f'{current_user.id}:{current_user.username}'[:100]
    return 'anonymous' if has_request_context() else 'system'


def _changes(obj, names, creating):
    state = inspect(obj)
    result = {}
    for name in names:
        history = state.attrs[name].history
        if creating or history.has_changes():
            result[name] = [history.deleted[0] if history.deleted else None, getattr(obj, name)]
    return result


def _audit_changes(session, flush_context, instances):
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        creating, deleting = obj in session.new, obj in session.deleted
        if isinstance(obj, AppUser):
            kind, target = 'account', str(obj.id or obj.username)
            names = ('username', 'role', 'is_active_flag', 'company_id', 'mfa_enabled')
        elif isinstance(obj, (MetaPermission, MetaFieldPermission)):
            kind = 'permission.field' if isinstance(obj, MetaFieldPermission) else 'permission.form'
            target = f'{obj.role}:{obj.field_id if isinstance(obj, MetaFieldPermission) else obj.form_id}'
            names = ('access',)
        elif isinstance(obj, ApiToken):
            kind, target = 'token', f'{obj.user_id}:{obj.id or obj.name}'
            names = ('name', 'revoked')
        else:
            continue
        changes = _changes(obj, names, creating)
        if changes or deleting:
            action = 'created' if creating else 'deleted' if deleting else 'changed'
            session.add(SecurityEvent(actor=_actor(), event=f'{kind}.{action}',
                                      target=target[:255], details=json.dumps(changes)))


def install(app):
    if not event.contains(Session, 'before_flush', _audit_changes):
        event.listen(Session, 'before_flush', _audit_changes)

    @app.after_request
    def denied(response):
        protected = request.blueprint in {'api', 'user', 'portal', 'designer', 'studio', 'form_templates', 'auth'}
        if protected and response.status_code in (401, 403, 404):
            from .db import get_engine
            try:
                with Session(get_engine()) as session:
                    session.add(SecurityEvent(
                        actor=_actor(), event='access.denied', target=request.path[:255],
                        details=json.dumps({'method': request.method, 'status': response.status_code})))
                    session.commit()
            except Exception:
                logging.getLogger(__name__).exception('Could not record denied access')
        return response


def describe(entry):
    """Plain-language summary; keep the original event available for inspection."""
    try:
        changes = json.loads(entry.details)
    except (TypeError, ValueError):
        changes = {}
    if not isinstance(changes, dict):
        changes = {}
    if entry.event == 'access.denied':
        return f"Access denied ({changes.get('status', 'unknown status')})"
    if entry.event.startswith('permission.'):
        pair = changes.get('access')
        if isinstance(pair, list) and len(pair) == 2:
            before, after = pair
            return f"{'Field' if '.field.' in entry.event else 'Form'} access: {str(before or 'Default').title()} → {str(after or 'None').title()}"
    if entry.event == 'account.changed':
        pair = changes.get('is_active_flag')
        if isinstance(pair, list) and len(pair) == 2:
            return 'Account enabled' if pair[1] else 'Account disabled'
        if 'role' in changes:
            return 'Account role changed'
    if entry.event == 'token.changed' and changes.get('revoked') == [False, True]:
        return 'API token revoked'
    return {'account.created': 'Account created', 'account.deleted': 'Account deleted',
            'account.changed': 'Account settings changed', 'token.created': 'API token created',
            'token.changed': 'API token updated', 'token.deleted': 'API token deleted'}.get(
                entry.event, entry.event.replace('.', ' ').replace('_', ' ').capitalize())
