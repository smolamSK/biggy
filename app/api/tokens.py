"""Mint and hash API bearer tokens.

The plaintext secret is shown to the user exactly once (on creation); only its
sha256 is persisted, so a leaked database row can't be replayed as a token.
"""
import hashlib
import secrets

from ..metadata.models import ApiToken

PREFIX = "biggy_"


def hash_token(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def mint(session, user_id, name):
    """Create a token for ``user_id``. Returns ``(ApiToken, raw_secret)``."""
    raw = PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user_id, name=name or "token",
        token_hash=hash_token(raw), prefix=raw[:12],
    )
    session.add(token)
    session.commit()
    return token, raw
