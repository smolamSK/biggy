"""Designer-only draft form builder and non-writing role previews."""
import json
from types import SimpleNamespace

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import select
from werkzeug.datastructures import MultiDict

from .. import form_design
from ..access import can_read, can_write, field_access, form_access
from ..db import SessionLocal, engine_for_table
from ..forms.builder import build_form
from ..helpers import designer_required
from ..metadata.models import AppUser, FormRevision, MetaForm, MetaRelation, Role

bp = Blueprint('studio', __name__, url_prefix='/designer')


@bp.before_request
@login_required
@designer_required
def guard():
    pass


def fields_for(form):
    return [{'id':f.id, 'name':f.phys_name, 'label':f.label, 'type':f.data_type,
             'nullable':f.nullable, 'related_table_id':f.related_table_id,
             'choices':json.loads(f.enum_options or '[]') if f.data_type == 'enum' else []}
            for f in form.table.fields]


def response_data(session, form, draft):
    config = json.loads(draft.config_json)
    try:
        issues = form_design.validate_config(session, form, config)
    except (ValueError, TypeError, KeyError) as exc:
        issues = [{'level':'error', 'message':str(exc)}]
    revisions = session.scalars(select(FormRevision).where(FormRevision.form_id == form.id)
                                .order_by(FormRevision.id.desc()).limit(30)).all()
    return {'config':config, 'version':draft.version, 'issues':issues,
            'changes':form_design.comparison(form_design.snapshot(form), config),
            'stale':draft.base_hash != form_design.live_hash(form),
            'tested':draft.tested_hash == form_design.digest(config),
            'revisions':[{'id':r.id,'label':f'{r.description} · {r.published_at}'} for r in revisions]}


@bp.get('/forms/<int:form_id>/builder')
def builder(form_id):
    session = SessionLocal()
    form = session.scalar(select(MetaForm).where(MetaForm.id == form_id).with_for_update())
    if not form:
        abort(404)
    draft = form_design.get_draft(session, form, current_user.id)
    relations = session.scalars(select(MetaRelation).where(MetaRelation.kind == 'mn')).all()
    related_fields = {}
    from ..metadata.models import MetaTable
    for field in form.table.fields:
        if field.related_table_id:
            target = session.get(MetaTable, field.related_table_id)
            if target:
                related_fields[str(field.id)] = [{'id':f.id,'label':f.label,'related_table_id':f.related_table_id}
                                                for f in target.fields if f.data_type == 'relation']
    data = {**response_data(session, form, draft), 'fields':fields_for(form),
            'related_fields':related_fields,
            'relations':[{'id':r.id,'name':r.name} for r in relations
                         if form.table_id in (r.from_table_id,r.to_table_id)],
            'roles':[{'name':r.name,'label':r.label} for r in session.scalars(select(Role))],
            'users':[{'id':u.id,'label':u.username,'role':u.role} for u in session.scalars(
                select(AppUser).where(AppUser.is_active_flag.is_(True)).order_by(AppUser.username))]}
    session.commit()
    return render_template('designer/form_builder.html', mf=form, data=data)


def preview(session, form, config, payload):
    candidate = form_design.virtual(form, config)
    role = payload.get('role', 'user')
    if not session.scalar(select(Role).where(Role.name == role)):
        raise ValueError('Choose an existing role.')
    uid = payload.get('user_id')
    account = session.get(AppUser, uid) if type(uid) is int and uid else None
    if uid and (not account or not account.is_active or account.role != role):
        raise ValueError('Choose an active account with the selected role.')
    user = SimpleNamespace(id=account.id if account else 0, role=role,
                           company_id=account.company_id if account else None,
                           is_designer=role == 'designer', is_portal=role == 'portal',
                           is_authenticated=True, is_active=True)
    allowed = (bool(candidate.in_catalog and (form.table.track_audit or form.table.row_owned))
               if user.is_portal else can_read(form_access(session, user, form.id)))
    if not allowed:
        return {'allowed':False, 'valid':False, 'fields':[], 'message':'This role cannot open this form.'}
    sample = payload.get('sample', {})
    if not isinstance(sample, dict) or len(sample) > 200 or any(
            not (isinstance(v, (str, int, float, bool, type(None))) or
                 isinstance(v, list) and len(v) <= 500 and all(isinstance(x, (str, int)) for x in v))
            for v in sample.values()):
        raise ValueError('Sample values must be simple field values.')
    from ..form_rules import text
    built = build_form(candidate, session, engine_for_table(form.table), user)
    formdata = MultiDict()
    for key, value in sample.items():
        for entry in value if isinstance(value, list) else [value]:
            formdata.add(key, text(entry))
    form_instance = built.form_class(formdata=formdata,
                                     meta={'csrf':False})
    writable = candidate.purpose != 'view' and (user.is_portal or can_write(form_access(session, user, form.id)))
    valid = form_instance.validate() if writable else True
    result = []
    for definition in candidate.items:
        field = next((f for f in form.table.fields if f.id == definition.field_id), None)
        if not field:
            continue
        access = field_access(session, user, field)
        item = next((i for i in built.items if i.name == field.phys_name), None)
        state = form_instance.rule_states.get(field.phys_name, {'visible':bool(item), 'readonly':True, 'required':False})
        if not writable:
            state = {**state, 'readonly':True, 'required':False}
        reason = ('Hidden by field permissions' if not item else 'Show condition is false' if not state['visible']
                  else 'Read-only permission or form rule' if state['readonly'] else
                  'Required by field settings or matching rule' if state['required'] else 'Visible and optional')
        control = form_instance._fields.get(field.phys_name)
        options, choice_count = [], 0
        if control is not None and hasattr(control, 'choices'):
            choices = list(control.choices)
            parent = (control.render_kw or {}).get('data-parent-field')
            if parent and sample.get(parent) not in (None, ''):
                choices = [c for c in choices if len(c) < 3 or c[2].get('data-parent') in (None,'',str(sample[parent]))]
            choice_count = len([c for c in choices if c[0] not in (None,'')])
            options = [{'value':c[0], 'label':c[1]} for c in choices[:100]]
            if parent:
                reason += f'. {choice_count} choices match the selected parent.'
        result.append({'name':field.phys_name,'label':definition.label_override or field.label,
                       'type':field.data_type,'access':access, **state, 'reason':reason,
                       'options':options,'choice_count':choice_count,
                       'errors':list(control.errors or []) if control is not None else []})
    for item in built.items:
        if item.kind != 'relation_mn':
            continue
        control = form_instance._fields[item.name]
        result.append({'name':item.name, 'label':item.label, 'type':'relation_mn',
                       'access':'read' if item.readonly else 'write', 'visible':True,
                       'readonly':item.readonly or not writable, 'required':bool(control.flags.required) and writable,
                       'reason':'Choose related records; the preview does not save links.',
                       'options':[{'value':c[0], 'label':c[1]} for c in control.choices[:100]],
                       'choice_count':len(control.choices), 'errors':list(control.errors or [])})
    return {'allowed':True, 'valid':valid, 'fields':result, 'message':
            'Preview only: no records or attachments were created. Choose an account to test organization-specific choices.'}


@bp.post('/forms/<int:form_id>/builder/<action>')
def action(form_id, action):
    session = SessionLocal()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error='Expected a JSON object.'), 400
    try:
        form, draft = form_design.lock_draft(session, form_id, body.get('version'))
        if action == 'save':
            config = body.get('config')
            form_design.validate_config(session, form, config)
            draft.config_json = form_design.dump(config)
            draft.version += 1
            draft.tested_hash = None
            draft.updated_by = current_user.id
            session.commit()
        elif action == 'preview':
            config = json.loads(draft.config_json)
            issues = form_design.validate_config(session, form, config)
            result = preview(session, form, config, body)
            if result['valid'] and not any(i['level'] == 'error' for i in issues):
                draft.tested_hash = form_design.digest(config)
            else:
                draft.tested_hash = None
            draft.version += 1
            session.commit()
            return jsonify(**response_data(session, form, draft), preview=result)
        elif action == 'publish':
            form_design.publish(session, form, draft, current_user)
        elif action in {'reset','restore'}:
            config = form_design.snapshot(form)
            if action == 'restore':
                if type(body.get('revision_id')) is not int:
                    raise ValueError('Choose a valid revision.')
                revision = session.get(FormRevision, body['revision_id'])
                if not revision or revision.form_id != form_id:
                    raise ValueError('Choose a revision belonging to this form.')
                config = json.loads(revision.config_json)
                form_design.validate_config(session, form, config)
            draft.config_json = form_design.dump(config)
            draft.base_hash = form_design.live_hash(form)
            draft.version += 1
            draft.tested_hash = None
            session.commit()
        else:
            abort(404)
        return jsonify(response_data(session, form, draft))
    except (ValueError, TypeError, KeyError) as exc:
        session.rollback()
        return jsonify(error=str(exc)), 409 if 'changed' in str(exc) else 400


@bp.get('/tables/<int:table_id>/impact')
@bp.get('/tables/<int:table_id>/fields/<int:field_id>/impact')
@bp.get('/relations/<int:relation_id>/impact')
def schema_impact(table_id=None, field_id=None, relation_id=None):
    from .. import schema_impact as impact
    from ..metadata.models import MetaField, MetaTable
    session = SessionLocal()
    relation = session.get(MetaRelation, relation_id) if relation_id else None
    if relation_id and not relation:
        abort(404)
    table = session.get(MetaTable, relation.from_table_id if relation else table_id)
    if not table:
        abort(404)
    field = session.get(MetaField, relation.from_field_id if relation and relation.kind == 'm1'
                        else field_id) if field_id or relation and relation.from_field_id else None
    if field_id and (not field or field.table_id != table.id):
        abort(404)
    groups, notes = impact.dependencies(session, table, field, relation)
    counts = impact.record_counts(session, table, field, relation) if request.args.get('counts') == '1' else None
    return render_template('designer/schema_impact.html', table=table, field=field,
                           relation=relation, groups=groups, notes=notes, counts=counts,
                           dependency_count=sum(len(g['entries']) for g in groups))
