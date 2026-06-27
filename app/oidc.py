"""OpenID Connect (authorization-code) client for SSO login.

Discovers the provider, builds the authorize URL, exchanges the code, and — the
security-critical part — verifies the ID token: RS256 signature against the
provider's JWKS, plus ``iss`` / ``aud`` / ``exp`` / ``nonce`` claims. HTTP goes
through a swappable :data:`TRANSPORT` (mirroring :mod:`app.connectors`) so tests can
drive a stub IdP without sockets; signatures are verified with ``cryptography`` (no
new dependency). The user mapping/login lives in :mod:`app.auth.routes`.
"""
import base64
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode

from flask import current_app


class OidcError(Exception):
    """Any failure discovering, exchanging, or verifying — never leaks a session."""


# --------------------------------------------------------------------------- #
# Swappable transport (tests inject a fake IdP)
# --------------------------------------------------------------------------- #
def _urllib_transport(method, url, headers, body):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


TRANSPORT = _urllib_transport


def set_transport(fn):
    """Override the HTTP transport (used by tests). ``None`` restores urllib."""
    global TRANSPORT
    TRANSPORT = fn or _urllib_transport


_DISCO_CACHE = {}
_JWKS_CACHE = {}


def reset_caches():
    _DISCO_CACHE.clear()
    _JWKS_CACHE.clear()


def _get_json(url):
    status, text = TRANSPORT("GET", url, {"Accept": "application/json"}, None)
    if status != 200:
        raise OidcError(f"GET {url} -> {status}")
    try:
        return json.loads(text)
    except ValueError as exc:
        raise OidcError(f"bad JSON from {url}") from exc


def _cfg(key, default=None):
    return current_app.config.get(key, default)


# --------------------------------------------------------------------------- #
# Discovery / JWKS
# --------------------------------------------------------------------------- #
def discovery():
    issuer = (_cfg("OIDC_ISSUER") or "").rstrip("/")
    if not issuer:
        raise OidcError("OIDC_ISSUER is not configured")
    if issuer not in _DISCO_CACHE:
        _DISCO_CACHE[issuer] = _get_json(issuer + "/.well-known/openid-configuration")
    return _DISCO_CACHE[issuer]


def _jwks(jwks_uri, force=False):
    if force or jwks_uri not in _JWKS_CACHE:
        _JWKS_CACHE[jwks_uri] = _get_json(jwks_uri)
    return _JWKS_CACHE[jwks_uri]


def _b64url_decode(s):
    s = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def _public_key(jwks, kid):
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers

    for k in jwks.get("keys", []):
        if k.get("kty") == "RSA" and (kid is None or k.get("kid") == kid):
            n = int.from_bytes(_b64url_decode(k["n"]), "big")
            e = int.from_bytes(_b64url_decode(k["e"]), "big")
            return RSAPublicNumbers(e, n).public_key()
    return None


# --------------------------------------------------------------------------- #
# Authorize / token / verify
# --------------------------------------------------------------------------- #
def authorize_url(state, nonce, redirect_uri):
    d = discovery()
    params = {
        "response_type": "code",
        "client_id": _cfg("OIDC_CLIENT_ID"),
        "redirect_uri": redirect_uri,
        "scope": _cfg("OIDC_SCOPES", "openid email profile"),
        "state": state,
        "nonce": nonce,
    }
    return d["authorization_endpoint"] + "?" + urlencode(params)


def exchange_code(code, redirect_uri):
    d = discovery()
    cid, secret = _cfg("OIDC_CLIENT_ID"), _cfg("OIDC_CLIENT_SECRET") or ""
    body = urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode("ascii")
    basic = base64.b64encode(f"{cid}:{secret}".encode("utf-8")).decode("ascii")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json", "Authorization": "Basic " + basic}
    status, text = TRANSPORT("POST", d["token_endpoint"], headers, body)
    if status != 200:
        raise OidcError(f"token endpoint -> {status}: {text[:200]}")
    try:
        tokens = json.loads(text)
    except ValueError as exc:
        raise OidcError("bad token response") from exc
    if not tokens.get("id_token"):
        raise OidcError("no id_token in token response")
    return tokens


def verify_id_token(id_token, nonce):
    """Verify signature + claims; return the claims dict or raise :class:`OidcError`."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    parts = id_token.split(".")
    if len(parts) != 3:
        raise OidcError("malformed id_token")
    header = json.loads(_b64url_decode(parts[0]))
    claims = json.loads(_b64url_decode(parts[1]))
    sig = _b64url_decode(parts[2])
    if header.get("alg") != "RS256":   # never accept 'none' / HS algorithms
        raise OidcError(f"unexpected alg {header.get('alg')!r}")

    d = discovery()
    kid = header.get("kid")
    key = _public_key(_jwks(d["jwks_uri"]), kid)
    if key is None:                    # unknown kid → refetch once (key rotation)
        key = _public_key(_jwks(d["jwks_uri"], force=True), kid)
    if key is None:
        raise OidcError("no matching signing key")

    signing_input = (parts[0] + "." + parts[1]).encode("ascii")
    try:
        key.verify(sig, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise OidcError("bad signature") from exc

    if claims.get("iss") != d.get("issuer", _cfg("OIDC_ISSUER", "").rstrip("/")):
        raise OidcError("issuer mismatch")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if _cfg("OIDC_CLIENT_ID") not in auds:
        raise OidcError("audience mismatch")
    now = int(time.time())
    if int(claims.get("exp", 0)) < now - 30:
        raise OidcError("token expired")
    if int(claims.get("iat", now)) > now + 300:
        raise OidcError("token issued in the future")
    if nonce and claims.get("nonce") != nonce:
        raise OidcError("nonce mismatch")
    return claims
