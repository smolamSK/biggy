"""Designer template library and guided, draft-only setup."""
import json

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select

from .. import form_design, form_templates
from ..db import SessionLocal
from ..helpers import designer_required
from ..metadata.models import (
    FormDraft,
    FormTemplate,
    MetaFieldPermission,
    MetaForm,
    MetaPermission,
    MetaTable,
    Role,
    SecurityEvent,
)

bp = Blueprint('form_templates', __name__, url_prefix='/designer/form-templates')


@bp.before_request
@login_required
@designer_required
def guard():
    pass


def get_template(session, key):
    if key in form_templates.BUILTINS:
        return form_templates.BUILTINS[key]
    if not key.startswith('saved-') or not key[6:].isdigit():
        abort(404)
    saved = session.get(FormTemplate, int(key[6:]))
    if not saved:
        abort(404)
    return {'name': saved.name, 'description': saved.description, 'config': json.loads(saved.config_json)}


@bp.get('')
def library():
    session = SessionLocal()
    templates = [{'key': key, 'builtin': True, **value} for key, value in form_templates.BUILTINS.items()]
    templates.extend({'key': f'saved-{t.id}', 'id': t.id, 'name': t.name,
                      'description': t.description, 'builtin': False, 'created_at': t.created_at}
                     for t in session.scalars(select(FormTemplate).order_by(FormTemplate.name, FormTemplate.id)))
    return render_template('designer/form_templates.html', templates=templates)


@bp.route('/save/<int:form_id>', methods=['GET', 'POST'])
def save(form_id):
    session = SessionLocal()
    form = session.scalar(select(MetaForm).where(MetaForm.id == form_id).with_for_update())
    if not form:
        abort(404)
    draft = session.scalar(select(FormDraft).where(FormDraft.form_id == form_id))
    error = None
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            description = request.form.get('description', '').strip()
            if not name or len(name) > 160 or len(description) > 2000:
                raise ValueError('Enter a template name (up to 160 characters) and a description up to 2,000 characters.')
            if request.form.get('live_hash') != form_design.live_hash(form):
                raise ValueError('The source form changed. Reload before saving the template.')
            config = form_design.snapshot(form)
            source = request.form.get('source')
            if source == 'draft':
                if not draft or str(draft.version) != request.form.get('version'):
                    raise ValueError('The saved draft changed. Reload before saving the template.')
                config = json.loads(draft.config_json)
            elif source != 'live':
                raise ValueError('Choose the live form or its saved draft.')
            template = FormTemplate(name=name, description=description,
                                    config_json=form_design.dump(form_templates.capture(session, form, config)),
                                    created_by=current_user.id)
            session.add(template)
            session.flush()
            session.add(SecurityEvent(actor=f'{current_user.id}:{current_user.username}',
                                      event='form_template.created', target=str(template.id),
                                      details=form_design.dump({'source_form_id': form.id})))
            session.commit()
            flash('Reusable template saved. It is independent of the source form.', 'success')
            return redirect(url_for('form_templates.library'))
        except (ValueError, TypeError, KeyError) as exc:
            session.rollback()
            error = str(exc)
    return render_template('designer/form_template_save.html', mf=form, draft=draft, error=error,
                           live_hash=form_design.live_hash(form)), 400 if error else 200


@bp.post('/<int:template_id>/delete')
def delete(template_id):
    session = SessionLocal()
    template = session.get(FormTemplate, template_id)
    if not template:
        abort(404)
    session.delete(template)
    session.add(SecurityEvent(actor=f'{current_user.id}:{current_user.username}',
                              event='form_template.deleted', target=str(template_id), details='{}'))
    session.commit()
    flash('Template deleted. Forms and saved drafts remain unchanged.', 'success')
    return redirect(url_for('form_templates.library'))


@bp.get('/<key>/setup')
def setup(key):
    session = SessionLocal()
    template = get_template(session, key)
    forms = session.scalars(select(MetaForm).order_by(MetaForm.title)).all()
    selected = request.args.get('form_id', type=int)
    form = next((f for f in forms if f.id == selected), None)
    data = None
    if form:
        fields, relations = form_templates.choices(session, form, template['config'])
        related = {}
        for field in form.table.fields:
            if field.data_type == 'relation' and field.related_table_id:
                target = session.get(MetaTable, field.related_table_id)
                related[str(field.id)] = [{'id': f.id, 'label': f.label, 'name': f.phys_name,
                                          'target': f.related_table_id}
                                         for f in target.fields if f.data_type == 'relation'] if target else []
        data = {'template': template['config'], 'fields': fields, 'relations': relations,
                'related': related, 'targets': {str(f.id): f.related_table_id for f in form.table.fields},
                'form_id': form.id, 'review_url': url_for('form_templates.review', key=key, form_id=form.id),
                'apply_url': url_for('form_templates.apply', key=key, form_id=form.id)}
    return render_template('designer/form_template_setup.html', template=template, forms=forms,
                           selected=form, data=data, key=key)


def permissions(session, form):
    form_perms = {p.role: p.access for p in session.scalars(select(MetaPermission).where(MetaPermission.form_id == form.id))}
    field_perms = session.scalars(select(MetaFieldPermission).where(
        MetaFieldPermission.field_id.in_([f.id for f in form.table.fields]))).all()
    roles = session.scalars(select(Role).order_by(Role.name)).all()
    summary = [{'role': r.label, 'access': 'Full access' if r.name == 'designer' else
                'Catalog and ownership rules' if r.name == 'portal' else form_perms.get(r.name, 'write'),
                'restricted_fields': sum(p.role == r.name and p.access != 'write' for p in field_perms)} for r in roles]
    fingerprint = form_design.digest({'forms': sorted(form_perms.items()),
        'fields': sorted((p.role, p.field_id, p.access) for p in field_perms),
        'roles': [(r.name, r.label) for r in roles]})
    return summary, fingerprint


def prepare(session, key, form_id):
    form = session.scalar(select(MetaForm).where(MetaForm.id == form_id).with_for_update())
    if not form:
        abort(404)
    template = get_template(session, key)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValueError('Expected field and relationship mappings.')
    config, issues = form_templates.map_config(session, form, template['config'],
        body.get('fields'), body.get('relations'), body.get('filters', {}))
    draft = session.scalar(select(FormDraft).where(FormDraft.form_id == form_id).with_for_update())
    summary, perm_hash = permissions(session, form)
    stamp = {'user': current_user.id, 'form': form.id, 'template': key,
             'template_hash': form_design.digest(template['config']), 'config': form_design.digest(config),
             'live': form_design.live_hash(form), 'draft': draft.version if draft else 0, 'permissions': perm_hash}
    return form, draft, config, issues, summary, stamp, body


def signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='form-template-review')


@bp.post('/<key>/review/<int:form_id>')
def review(key, form_id):
    session = SessionLocal()
    try:
        form, draft, config, issues, summary, stamp, _body = prepare(session, key, form_id)
        replace = bool(draft and json.loads(draft.config_json) != form_design.snapshot(form))
        result = {'config': config, 'issues': issues, 'permissions': summary,
                  'changes': form_design.comparison(form_design.snapshot(form), config),
                  'replace_draft': replace, 'token': signer().dumps(stamp),
                  'field_labels': {f.phys_name: f.label for f in form.table.fields}}
        session.rollback()  # review creates nothing and releases locks
        return jsonify(result)
    except (ValueError, TypeError, KeyError) as exc:
        session.rollback()
        return jsonify(error=str(exc)), 400


@bp.post('/<key>/apply/<int:form_id>')
def apply(key, form_id):
    session = SessionLocal()
    try:
        form, draft, config, _issues, _summary, stamp, body = prepare(session, key, form_id)
        try:
            reviewed = signer().loads(body.get('token', ''), max_age=3600)
        except (BadSignature, TypeError):
            raise ValueError('Review these mappings and permissions before creating the draft.') from None
        if reviewed != stamp:
            raise ValueError('The form, draft, mappings or permissions changed. Review again before applying.')
        if draft and json.loads(draft.config_json) != form_design.snapshot(form) and body.get('replace_draft') is not True:
            raise ValueError('Choose whether to replace the existing saved draft.')
        if draft is None:
            draft = FormDraft(form_id=form.id, version=1)
            session.add(draft)
        else:
            draft.version += 1
        draft.config_json = form_design.dump(config)
        draft.base_hash = form_design.live_hash(form)
        draft.tested_hash = None
        draft.updated_by = current_user.id
        session.add(SecurityEvent(actor=f'{current_user.id}:{current_user.username}',
                                  event='form_template.applied', target=str(form.id),
                                  details=form_design.dump({'template': key})))
        session.commit()
        return jsonify(url=url_for('studio.builder', form_id=form.id))
    except (ValueError, TypeError, KeyError) as exc:
        session.rollback()
        return jsonify(error=str(exc)), 409 if 'changed' in str(exc) else 400
