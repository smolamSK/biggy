"""Application-layer encryption for secret columns (at rest).

A single :class:`EncryptedText` SQLAlchemy ``TypeDecorator`` encrypts on write and
decrypts on read, so secret columns (connection tokens, data-source passwords,
webhook HMAC secrets, pull-source auth) are ciphertext in the database but plaintext
to every ORM consumer — transparent to the read/write sites and to schema export.

Encryption is Fernet (AES-128-CBC + HMAC, authenticated). The key is
``BIGGY_ENCRYPTION_KEY`` (a urlsafe-base64 Fernet key) when set, otherwise a stable
key derived from ``SECRET_KEY``. **Rotating the key (or SECRET_KEY when no dedicated
key is set) makes existing ciphertext unreadable** — back up / re-key deliberately.

Reads fall back to the raw stored value when it isn't a valid token, so databases
written before encryption was enabled keep working; those values get encrypted on
their next write (or via the ``encrypt-secrets`` CLI).
"""
import base64
import hashlib

from flask import current_app
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

_FERNET_CACHE = {}


def _key():
    """The Fernet key bytes: the configured key, else derived from SECRET_KEY."""
    raw = current_app.config.get("BIGGY_ENCRYPTION_KEY")
    if raw:
        return raw.encode("utf-8") if isinstance(raw, str) else raw
    secret = (current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    digest = hashlib.sha256(b"biggy-secret-encryption:" + secret).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet():
    from cryptography.fernet import Fernet

    key = _key()
    f = _FERNET_CACHE.get(key)
    if f is None:
        f = _FERNET_CACHE[key] = Fernet(key)
    return f


def encrypt(value):
    """Plaintext str -> token str (``None`` passes through)."""
    if value is None:
        return None
    return _fernet().encrypt(str(value).encode("utf-8")).decode("ascii")


def decrypt(value):
    """Token str -> plaintext str; returns the input unchanged if it isn't a token."""
    if value is None:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(str(value).encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return value  # legacy plaintext written before encryption was enabled


class EncryptedText(TypeDecorator):
    """TEXT column whose value is Fernet-encrypted at rest, transparent to the ORM."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
