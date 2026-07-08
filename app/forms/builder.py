"""Dynamically build a WTForms form from a stored form definition.

A :class:`BuiltForm` carries the generated form class plus an ordered list of
:class:`FormItem` describing each control, so routes can both render the form
and map submitted data back to physical columns / many-to-many link sets.
"""
import json
from dataclasses import dataclass
from dataclasses import field as dc_field

from flask_wtf import FlaskForm
from sqlalchemy import select
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    FloatField,
    IntegerField,
    SelectField,
    SelectMultipleField,
    StringField,
    TextAreaField,
)
from wtforms.fields import DateTimeLocalField, TimeField
from wtforms.validators import (
    InputRequired,
    Length,
    NumberRange,
    Optional,
    Regexp,
    ValidationError,
)

from .. import data_service
from ..metadata.field_types import FILE_TYPES, RELATION_TYPE
from ..metadata.models import AppUser, MetaField, MetaRelation, MetaTable


def _valid_json(form, field):
    if field.data:
        try:
            json.loads(field.data)
        except (ValueError, TypeError):
            raise ValidationError("Enter valid JSON.")

_NONE = ""  # select value representing "no selection"


def _fk_coerce(value):
    if value in (None, "", "None"):
        return None
    return int(value)


def _str_fk_coerce(value):
    return None if value in (None, "", "None") else str(value)


def _target_pk_is_int(session, target):
    """Whether the relation target's primary key is integer-typed."""
    if target.pk_col == "id":
        return True
    f = next((x for x in target.fields if x.phys_name == target.pk_col), None)
    return f is None or f.data_type in ("integer", "bigint")


@dataclass
class FormItem:
    name: str                 # attribute name on the form
    label: str
    kind: str                 # 'field' | 'relation_m1' | 'relation_mn'
    help_text: str = ""
    readonly: bool = False
    column: str | None = None  # physical column (field / m1)
    # many-to-many wiring
    junction: str | None = None
    this_col: str | None = None
    other_col: str | None = None
    meta: object = dc_field(default=None, repr=False)


@dataclass
class BuiltForm:
    form_class: type
    items: list


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _text_rules(meta):
    rules = []
    lmin = meta.min_length if meta.min_length is not None else -1
    lmax = meta.max_length if meta.max_length is not None else (meta.length or -1)
    if lmin != -1 or lmax != -1:
        rules.append(Length(min=lmin, max=lmax))
    if meta.pattern:
        rules.append(Regexp(meta.pattern, message="Does not match the required format."))
    return rules


def _number_rules(meta):
    nmin, nmax = _num(meta.min_value), _num(meta.max_value)
    return [NumberRange(min=nmin, max=nmax)] if (nmin is not None or nmax is not None) else []


_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
_URL_RE = r"^https?://\S+$"
_PHONE_RE = r"^[+(]?[\d][\d\s().-]{4,}$"


def _scalar_field(meta: MetaField, label, required, render_kw=None):
    validators = [InputRequired()] if required else [Optional()]
    rk = dict(render_kw or {})
    dt = meta.data_type
    if dt == "string":
        validators += _text_rules(meta)
        return StringField(label, validators=validators, render_kw=rk or None)
    if dt in ("text", "markdown"):
        validators += _text_rules(meta)
        return TextAreaField(label, validators=validators, render_kw=rk or None)
    if dt in ("integer", "bigint"):
        validators += _number_rules(meta)
        return IntegerField(label, validators=validators, render_kw=rk or None)
    if dt in ("decimal", "currency", "percent"):
        validators += _number_rules(meta)
        return DecimalField(label, places=meta.scale if meta.scale is not None else 2,
                            validators=validators, render_kw=rk or None)
    if dt == "float":
        validators += _number_rules(meta)
        return FloatField(label, validators=validators, render_kw=rk or None)
    if dt == "boolean":
        return BooleanField(label, validators=[Optional()], render_kw=rk or None)
    if dt == "date":
        return DateField(label, validators=validators, render_kw=rk or None)
    if dt == "datetime":
        return DateTimeLocalField(label, format="%Y-%m-%dT%H:%M", validators=validators,
                                  render_kw=rk or None)
    if dt == "time":
        return TimeField(label, validators=validators, render_kw=rk or None)
    if dt == "email":
        validators.append(Regexp(_EMAIL_RE, message="Enter a valid email address."))
        rk.setdefault("type", "email")
        return StringField(label, validators=validators, render_kw=rk)
    if dt == "url":
        validators.append(Regexp(_URL_RE, message="Enter a valid http(s) URL."))
        rk.setdefault("type", "url")
        return StringField(label, validators=validators, render_kw=rk)
    if dt == "phone":
        validators.append(Regexp(_PHONE_RE, message="Enter a valid phone number."))
        rk.setdefault("type", "tel")
        return StringField(label, validators=validators, render_kw=rk)
    if dt == "json":
        return TextAreaField(label, validators=validators + [_valid_json], render_kw=rk or None)
    if dt in ("autonumber", "formula"):
        rk["readonly"] = True
        return StringField(label, validators=[Optional()], render_kw=rk)
    if dt in ("enum", "tags"):
        opts = json.loads(meta.enum_options or "[]")
        choices = [(o, o) for o in opts]
        if dt == "tags":
            return SelectMultipleField(label, choices=choices, validators=[Optional()],
                                       render_kw=rk or None)
        if not required:
            choices = [(_NONE, "— none —")] + choices
        return SelectField(label, choices=choices, validators=validators, render_kw=rk or None)
    raise ValueError(f"Unsupported field type {dt!r}")


def build_form(meta_form, session, engine, user=None):
    """Return a :class:`BuiltForm` for the given :class:`MetaForm`.

    When ``user`` is a non-designer, field-level permissions apply: fields with
    ``none`` access are omitted entirely, ``read`` fields are marked read-only.
    """
    attrs = {}
    items = []
    fperm = {}
    if user is not None and not getattr(user, "is_designer", False):
        from ..helpers import _field_perm_map
        fperm = _field_perm_map(session, user)

    for it in meta_form.items:
        if it.kind == "section":
            items.append(FormItem(name=f"section_{it.id}", label=it.label_override or "Section",
                                  kind="section"))
            continue
        if it.kind == "field" and it.field_id:
            mf = session.get(MetaField, it.field_id)
            if not mf:
                continue
            facc = fperm.get(mf.id, "write")
            if facc == "none":
                continue                       # hidden by field permission
            label = it.label_override or mf.label
            required = it.required or not mf.nullable
            locked = it.readonly or facc == "read" or mf.data_type in ("autonumber", "formula")

            if mf.data_type in FILE_TYPES:
                # virtual upload field — managed outside WTForms via attachments
                items.append(FormItem(name=mf.phys_name, label=label, kind="file",
                                      help_text=it.help_text or "", readonly=locked,
                                      column=mf.phys_name, meta=mf))
                continue

            if mf.data_type == RELATION_TYPE:
                target, disp_cols = m1_target_and_columns(session, mf)
                parent_name, match_field = _dependency(session, it, target)
                render_kw = None
                if parent_name and match_field:
                    opts = data_service.load_options_with(
                        engine, target.phys_name, disp_cols, match_field.phys_name)
                    choices = [(str(i), lbl, {"data-parent": "" if pv is None else str(pv)})
                               for i, lbl, pv in opts]
                    if not required:
                        choices = [(_NONE, "— none —", {})] + choices
                    render_kw = {"data-parent-field": parent_name}
                else:
                    opts = data_service.load_options(engine, target.phys_name, disp_cols)
                    choices = [(str(i), lbl) for i, lbl in opts]
                    if not required:
                        choices = [(_NONE, "— none —")] + choices
                    # type-to-filter enhancement (static/pickers.js); dependent
                    # pickers are managed by dependent.js instead
                    render_kw = {"data-picker": "1"}
                if locked:
                    render_kw = dict(render_kw or {}, disabled=True)
                coerce = _fk_coerce if _target_pk_is_int(session, target) else _str_fk_coerce
                field = SelectField(
                    label, choices=choices, coerce=coerce,
                    validators=[InputRequired()] if required else [Optional()],
                    render_kw=render_kw,
                )
                items.append(FormItem(name=mf.phys_name, label=label, kind="relation_m1",
                                      help_text=it.help_text or "", readonly=locked,
                                      column=mf.phys_name, meta=mf))
                attrs[mf.phys_name] = field
            elif mf.data_type == "user":
                # references an app account (assignee); needs the session for choices
                choices = [(str(u.id), u.username) for u in session.scalars(
                    select(AppUser).where(AppUser.is_active_flag.is_(True))
                    .order_by(AppUser.username))]
                if not required:
                    choices = [(_NONE, "— none —")] + choices
                rk = {"data-picker": "1"}
                if locked:
                    rk["disabled"] = True
                attrs[mf.phys_name] = SelectField(
                    label, choices=choices, coerce=_fk_coerce,
                    validators=[InputRequired()] if required else [Optional()],
                    render_kw=rk)
                items.append(FormItem(name=mf.phys_name, label=label, kind="field",
                                      help_text=it.help_text or "", readonly=locked,
                                      column=mf.phys_name, meta=mf))
            else:
                attrs[mf.phys_name] = _scalar_field(mf, label, required,
                                                    render_kw={"readonly": True} if locked else None)
                items.append(FormItem(name=mf.phys_name, label=label, kind="field",
                                      help_text=it.help_text or "", readonly=locked,
                                      column=mf.phys_name, meta=mf))

        elif it.kind == "relation" and it.relation_id:
            rel = session.get(MetaRelation, it.relation_id)
            if not rel or rel.kind != "mn":
                continue
            this_id = meta_form.table_id
            other_id = rel.to_table_id if rel.from_table_id == this_id else rel.from_table_id
            this_tbl = session.get(MetaTable, this_id)
            other_tbl = session.get(MetaTable, other_id)
            this_col = f"{this_tbl.phys_name}_id"
            other_col = f"{other_tbl.phys_name}_id"
            if this_col == other_col:  # self relation
                other_col = f"{other_tbl.phys_name}_id_2"
            json_ids = (rel.to_display_field_ids if other_id == rel.to_table_id
                        else rel.from_display_field_ids)
            disp_cols = display_columns(session, other_tbl, json_ids)
            opts = data_service.load_options(engine, other_tbl.phys_name, disp_cols)
            label = it.label_override or rel.name
            name = f"rel_{rel.id}"
            attrs[name] = SelectMultipleField(
                label, choices=[(str(i), lbl) for i, lbl in opts], coerce=int,
                validators=[Optional()], render_kw={"data-picker": "1"},
            )
            items.append(FormItem(name=name, label=label, kind="relation_mn",
                                  help_text=it.help_text or "", readonly=it.readonly,
                                  junction=rel.junction_phys_name,
                                  this_col=this_col, other_col=other_col, meta=rel))

    form_class = type("DynamicForm", (FlaskForm,), attrs)
    return BuiltForm(form_class=form_class, items=items)


def display_field_name(session, meta_table):
    """Physical column to show for a referenced table (its display field or first text)."""
    if meta_table is None:
        return "id"
    if meta_table.display_field_id:
        f = session.get(MetaField, meta_table.display_field_id)
        if f:
            return f.phys_name
    for f in meta_table.fields:
        if f.data_type in ("string", "text", "markdown"):
            return f.phys_name
    return "id"


def display_columns(session, meta_table, field_ids_json):
    """Resolve a relation's chosen display-field ids to physical column names.

    Falls back to the table's single display field when nothing is chosen.
    """
    if meta_table is None:
        return ["id"]
    ids = []
    if field_ids_json:
        try:
            ids = [int(x) for x in json.loads(field_ids_json)]
        except (ValueError, TypeError):
            ids = []
    if ids:
        by_id = {f.id: f.phys_name for f in meta_table.fields}
        cols = [by_id[i] for i in ids if i in by_id]
        if cols:
            return cols
    return [display_field_name(session, meta_table)]


def _dependency(session, item, target):
    """For a dependent relation item, return (controlling field name, match field) or (None, None).

    The match field is the explicitly chosen ``filter_field`` or, failing that, the single relation
    field on the target table that points at the controlling field's target table.
    """
    if not getattr(item, "parent_field_id", None):
        return None, None
    parent = session.get(MetaField, item.parent_field_id)
    if not parent or parent.data_type != RELATION_TYPE:
        return None, None
    match = session.get(MetaField, item.filter_field_id) if item.filter_field_id else None
    if not match:
        candidates = [f for f in target.fields if f.data_type == RELATION_TYPE
                      and f.related_table_id == parent.related_table_id]
        match = candidates[0] if len(candidates) == 1 else None
    return (parent.phys_name, match) if match else (None, None)


def m1_target_and_columns(session, meta_field):
    """For an M:1 relation field, return (target table, display column names)."""
    target = session.get(MetaTable, meta_field.related_table_id)
    rel = session.scalar(
        select(MetaRelation).where(MetaRelation.from_field_id == meta_field.id)
    )
    json_ids = rel.to_display_field_ids if rel else None
    return target, display_columns(session, target, json_ids)
