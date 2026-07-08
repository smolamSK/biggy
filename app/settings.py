"""Instance settings (branding) — UI-editable, with Config values as fallback.

Stored in the ``app_setting`` key-value table so adding a setting never needs a
migration. ``branding()`` is what templates consume (injected as a context
global); it is cheap (one small SELECT) and must never break rendering.
"""
from flask import current_app
from sqlalchemy import select

from .db import SessionLocal
from .metadata.models import AppSetting

#: the settings the Designer-mode Settings page edits
BRANDING_KEYS = ("app_name", "accent", "default_theme")
THEMES = ("light", "dark", "sepia", "ocean", "contrast")


def get_all(session):
    """All stored settings as a dict (blank values omitted)."""
    return {s.key: s.value for s in session.scalars(select(AppSetting))
            if s.value not in (None, "")}


def save(session, mapping):
    """Upsert the given settings; blank values delete the row (fall back)."""
    existing = {s.key: s for s in session.scalars(select(AppSetting))}
    for key, value in mapping.items():
        value = (value or "").strip()
        row = existing.get(key)
        if not value:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(AppSetting(key=key, value=value))
        else:
            row.value = value
    session.commit()


def branding():
    """Branding values for templates: stored settings over Config defaults."""
    out = {
        "app_name": current_app.config.get("APP_NAME", "Biggy"),
        "accent": "",
        "default_theme": "",
    }
    try:
        stored = get_all(SessionLocal())
    except Exception:  # noqa: BLE001 - a missing table must never break rendering
        return out
    for key in BRANDING_KEYS:
        if stored.get(key):
            out[key] = stored[key]
    if out["default_theme"] not in THEMES:
        out["default_theme"] = ""
    return out
