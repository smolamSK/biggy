"""TOTP two-factor authentication — pure standard library (RFC 6238 / RFC 4226).

Compatible with Google Authenticator / Authy / 1Password etc. No dependency: the
one-time code is an HMAC-SHA1 over a 30-second time counter, base32-encoded secret.
The secret is stored encrypted at rest (see :class:`app.crypto.EncryptedText`);
backup codes are stored as sha256 hashes and consumed on use.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
import time
from urllib.parse import quote

_STEP = 30      # seconds per TOTP window
_DIGITS = 6


def new_secret():
    """A fresh base32 TOTP secret (160 bits, no padding)."""
    return base64.b32encode(os.urandom(20)).decode("ascii").rstrip("=")


def _hotp(secret_b32, counter):
    # pad the base32 secret back to a multiple of 8 chars before decoding
    pad = "=" * ((8 - len(secret_b32) % 8) % 8)
    key = base64.b32decode(secret_b32.upper() + pad)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** _DIGITS)).zfill(_DIGITS)


def now_code(secret, at=None):
    """The current 6-digit TOTP code for ``secret``."""
    return _hotp(secret, int((at if at is not None else time.time()) // _STEP))


def verify(secret, code, window=1, at=None):
    """True if ``code`` matches within ±``window`` steps (clock-skew tolerance)."""
    if not secret or not code:
        return False
    code = str(code).strip()
    if len(code) != _DIGITS or not code.isdigit():
        return False
    counter = int((at if at is not None else time.time()) // _STEP)
    for drift in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, counter + drift), code):
            return True
    return False


def provisioning_uri(secret, username, issuer="Biggy"):
    """An ``otpauth://`` URI for authenticator-app enrollment (and QR generation)."""
    label = quote(f"{issuer}:{username}")
    return (f"otpauth://totp/{label}?secret={secret}"
            f"&issuer={quote(issuer)}&digits={_DIGITS}&period={_STEP}")


def qr_svg(data):
    """Inline SVG QR code for ``data`` (server-rendered, no network), or ``None``.

    Generated locally with ``qrcode`` so the secret never leaves the host. Falls back
    to ``None`` if the library is unavailable — the page then shows the URI + key.
    """
    if not data:
        return None
    try:
        import io
        import qrcode
        import qrcode.image.svg

        buf = io.BytesIO()
        qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
        svg = buf.getvalue().decode("utf-8")
        return svg.split("?>", 1)[-1].strip() if svg.startswith("<?xml") else svg
    except Exception:  # noqa: BLE001 - the QR is a convenience; never break enrollment
        return None


# --------------------------------------------------------------------------- #
# Backup codes (one-time; stored hashed)
# --------------------------------------------------------------------------- #
def _hash(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def make_backup_codes(n=10):
    """Return ``(plaintext_codes, json_of_hashes)``. Show the plaintext list once."""
    codes = ["-".join((secrets.token_hex(2), secrets.token_hex(2))) for _ in range(n)]
    return codes, json.dumps([_hash(c) for c in codes])


def consume_backup_code(stored_json, code):
    """Try to spend a backup code. Returns ``(ok, new_json)``."""
    if not stored_json or not code:
        return False, stored_json
    try:
        hashes = json.loads(stored_json)
    except (ValueError, TypeError):
        return False, stored_json
    h = _hash(str(code).strip())
    if h in hashes:
        hashes.remove(h)
        return True, json.dumps(hashes)
    return False, stored_json


def backup_count(stored_json):
    try:
        return len(json.loads(stored_json)) if stored_json else 0
    except (ValueError, TypeError):
        return 0
