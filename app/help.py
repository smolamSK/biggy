"""Render the bundled Markdown manuals (``docs/*-manual.md``) as in-app Help.

The manuals are authored by us (trusted), so the produced HTML is rendered with
``|safe`` in the template. ``markdown`` is an optional dependency: if it is not
installed the raw text is shown in a ``<pre>`` block so the Help page never 500s.
"""
from html import escape
from pathlib import Path

from flask import current_app

# topic -> (manual filename, page title). The whitelist also prevents any path
# traversal from the ``<topic>`` URL segment.
MANUALS = {
    "user": ("user-manual.md", "User manual"),
    "designer": ("designer-manual.md", "Designer manual"),
    "setup": ("setup-and-operations.md", "Setup & operations"),
    "developer": ("developer-guide.md", "Developer guide"),
    "schema": ("schema-json-format.md", "Schema JSON format"),
}

# Topics shown only to designers (build/operate/extend, not for end users).
DESIGNER_TOPICS = frozenset({"designer", "setup", "developer", "schema"})

try:  # optional dependency
    import markdown as _markdown
except ImportError:  # pragma: no cover - exercised only without the lib
    _markdown = None


def _docs_dir():
    # app.root_path is .../biggy/app; the manuals live in .../biggy/docs
    return Path(current_app.root_path).parent / "docs"


def render_manual(topic):
    """Return ``(title, html)`` for a known topic, or ``None`` if unknown."""
    entry = MANUALS.get(topic)
    if not entry:
        return None
    filename, title = entry
    try:
        text = (_docs_dir() / filename).read_text(encoding="utf-8")
    except OSError:
        return title, "<p class='muted'>This manual is not available.</p>"
    if _markdown is None:
        return title, f"<pre>{escape(text)}</pre>"
    html = _markdown.markdown(
        text, extensions=["fenced_code", "tables", "toc", "sane_lists"])
    return title, html
