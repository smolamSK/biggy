"""Local-disk storage for uploaded attachments (file/image fields).

Files are written under ``UPLOAD_FOLDER/<field_id>/<stored_name>`` where
``stored_name`` is server-generated (a random token + extension), so user input
never reaches the path. The bytes live here; metadata lives in the
``app_attachment`` table (see :class:`app.metadata.models.Attachment`).
"""
import os
import secrets

from flask import current_app
from werkzeug.utils import secure_filename

ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class UploadError(ValueError):
    """Raised when an upload is rejected (e.g. wrong type for an image field)."""


def _root():
    return current_app.config["UPLOAD_FOLDER"]


def _field_dir(field_id):
    path = os.path.join(_root(), str(field_id))
    os.makedirs(path, exist_ok=True)
    return path


def _ext(filename):
    return os.path.splitext(filename or "")[1].lower()


def save(file_storage, field):
    """Persist an uploaded file for ``field``; return its metadata dict.

    Raises :class:`UploadError` for an image field given a non-image file.
    """
    original = secure_filename(file_storage.filename or "") or "file"
    ext = _ext(original)
    if field.data_type == "image" and ext not in ALLOWED_IMAGE_EXT:
        raise UploadError(
            f"{original}: images must be one of {', '.join(sorted(ALLOWED_IMAGE_EXT))}")

    stored_name = secrets.token_hex(8) + ext
    dest = os.path.join(_field_dir(field.id), stored_name)
    file_storage.save(dest)
    return {
        "stored_name": stored_name,
        "original_name": original,
        "content_type": file_storage.mimetype or "application/octet-stream",
        "size": os.path.getsize(dest),
    }


def abs_path(field_id, stored_name):
    return os.path.join(_field_dir(field_id), stored_name)


def delete(field_id, stored_name):
    """Best-effort removal of a stored file (ignore if already gone)."""
    try:
        os.remove(abs_path(field_id, stored_name))
    except OSError:
        pass
