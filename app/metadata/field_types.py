"""Registry of scalar field types available in Designer mode.

Each entry describes how a logical type is presented in the UI and which
extra attributes it needs. The mapping to SQLAlchemy column types lives in
:mod:`app.metadata.schema_service`; the mapping to WTForms fields lives in
:mod:`app.forms.builder`. The special ``relation`` type (a many-to-one
foreign key) is created via the Relations UI, not the plain add-field form.
"""

# order matters: drives the select list in the UI
SCALAR_TYPES = {
    "string": {"label": "Text (short)", "needs_length": True, "default_length": 255},
    "text": {"label": "Text (long)"},
    "markdown": {"label": "Markdown (rich text)"},
    "integer": {"label": "Integer"},
    "bigint": {"label": "Big integer"},
    "decimal": {"label": "Decimal", "needs_precision": True},
    "float": {"label": "Float"},
    "boolean": {"label": "Boolean (yes/no)"},
    "date": {"label": "Date"},
    "datetime": {"label": "Date & time"},
    "time": {"label": "Time"},
    "enum": {"label": "Choice list (enum)", "needs_options": True},
    "tags": {"label": "Tags (multi-select)", "needs_options": True},
    "email": {"label": "Email", "needs_length": True, "default_length": 255},
    "url": {"label": "URL / link", "needs_length": True, "default_length": 255},
    "phone": {"label": "Phone", "needs_length": True, "default_length": 40},
    "currency": {"label": "Currency", "needs_precision": True},
    "percent": {"label": "Percent", "needs_precision": True},
    "json": {"label": "JSON"},
    "autonumber": {"label": "Auto-number (sequence)"},
    "formula": {"label": "Formula (computed)"},
    "image": {"label": "Image (upload, preview)", "is_file": True},
    "file": {"label": "File (upload, download)", "is_file": True},
}

# multi-value tag type (stored as a JSON array of enum options)
TAGS_TYPE = "tags"
NUMERIC_TYPES = frozenset({"integer", "bigint", "decimal", "float", "currency", "percent"})

# data_type value used for many-to-one foreign-key columns
RELATION_TYPE = "relation"

# Upload field types. Like RELATION_TYPE these are *virtual*: they have no
# physical column — their files live in the app_attachment table.
FILE_TYPES = frozenset({"image", "file"})

ALL_TYPES = set(SCALAR_TYPES) | {RELATION_TYPE}


def type_label(data_type):
    if data_type == RELATION_TYPE:
        return "Relation (many-to-one)"
    return SCALAR_TYPES.get(data_type, {}).get("label", data_type)


def is_file(data_type):
    """Whether this type is a file/image upload (stored as attachments)."""
    return data_type in FILE_TYPES


def is_text_search(data_type):
    """Whether 'contains' search makes sense for this type."""
    return data_type in {"string", "text", "markdown", "enum", "email", "url", "phone",
                         "json", "tags", "autonumber", "formula"}
