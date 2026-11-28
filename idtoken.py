"""Verifies tokens from the 6j0.org ID server. A port of idtoken.js from the 6j0.org Node apps.

Tokens are compact JWTs signed with Ed25519 ("EdDSA"). The public keys come from the ID server's JWKS
(ID_JWKS_URL, default ID_ORIGIN + /.well-known/jwks.json), cached and re-fetched when an unknown kid shows up.
A token is accepted once: iss, aud, exp, and iat must check out, and its jti is remembered until it expires.
Sign-in tokens also carry the nonce the page made. Deletion notices (a 6j0 ID was deleted) are checked the same
way, but have no nonce and carry an event instead, so neither kind can stand in for the other.

Python's standard library has no Ed25519, so this uses the `cryptography` package for the signature check.
"""

import base64
import binascii
import json
import threading
import time
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

JWKS_MIN_REFETCH_S = 60
CLOCK_SKEW_S = 30
DELETED_EVENT = "6j0/v1/identity-deleted"
USER_AGENT = "ShoppingList/0.1 (6j0.org sign-in)"


class IdTokenError(Exception):
    """The token is bad. The message is fit to show people."""


class JwksError(Exception):
    """The ID server's keys couldn't be loaded (a server problem, not a bad token)."""


_lock = threading.Lock()
_keys = {}  # kid → Ed25519PublicKey
_fetched_at = 0.0
_seen = {}  # jti → exp (seconds)


def _b64u(s):
    if not isinstance(s, str):
        raise ValueError("not a string")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _part(s):
    try:
        v = json.loads(_b64u(s))
    except (ValueError, binascii.Error):
        raise IdTokenError("Bad sign-in token") from None
    if not isinstance(v, dict):
        raise IdTokenError("Bad sign-in token")
    return v


def _refresh_keys(jwks_url):
    global _keys, _fetched_at
    with _lock:
        if time.monotonic() - _fetched_at < JWKS_MIN_REFETCH_S and _fetched_at:
            return
        _fetched_at = time.monotonic()
    req = urllib.request.Request(jwks_url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
    except Exception as e:  # network trouble, HTTP errors, bad JSON
        raise JwksError(f"Couldn't load the ID server's keys from {jwks_url}: {e}") from e
    nxt = {}
    listed = data.get("keys") if isinstance(data, dict) else None
    for k in listed if isinstance(listed, list) else []:
        if (isinstance(k, dict) and k.get("kty") == "OKP" and k.get("crv") == "Ed25519"
                and isinstance(k.get("kid"), str) and isinstance(k.get("x"), str)):
            try:
                nxt[k["kid"]] = Ed25519PublicKey.from_public_bytes(_b64u(k["x"]))
            except (ValueError, binascii.Error):
                continue
    with _lock:
        _keys = nxt


def _check(token, issuer, audience, jwks_url):
    """The checks every token gets: signature, iss, aud, exp, iat, sub, and jti. Returns the claims."""
    if not isinstance(token, str) or len(token) > 4096:
        raise IdTokenError("Bad sign-in token")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise IdTokenError("Bad sign-in token")
    h, b, s = parts
    head, claims = _part(h), _part(b)
    if head.get("alg") != "EdDSA":
        raise IdTokenError("Unexpected sign-in token algorithm")

    kid = head.get("kid")
    if not isinstance(kid, str) or kid not in _keys:
        _refresh_keys(jwks_url)
    key = _keys.get(kid) if isinstance(kid, str) else None
    if key is None:
        raise IdTokenError("Sign-in token was signed with an unknown key")
    try:
        key.verify(_b64u(s), f"{h}.{b}".encode("ascii"))
    except (InvalidSignature, ValueError, binascii.Error, UnicodeEncodeError):
        raise IdTokenError("Sign-in token signature check failed") from None

    now = int(time.time())
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E731
    if claims.get("iss") != issuer:
        raise IdTokenError("Sign-in token is from a different ID server")
    if claims.get("aud") != audience:
        raise IdTokenError("Sign-in token is for a different app")
    if not num(claims.get("exp")) or claims["exp"] + CLOCK_SKEW_S < now:
        raise IdTokenError("Sign-in token expired. Please try again.")
    if not num(claims.get("iat")) or claims["iat"] - CLOCK_SKEW_S > now:
        raise IdTokenError("Sign-in token is from the future")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub or len(sub) > 64:
        raise IdTokenError("Bad sign-in token subject")
    if not isinstance(claims.get("jti"), str) or not claims["jti"]:
        raise IdTokenError("Bad sign-in token")
    return claims


def _use(claims):
    """Accept a token once: remember its jti until it expires."""
    now = int(time.time())
    with _lock:
        for jti, exp in list(_seen.items()):
            if exp + CLOCK_SKEW_S < now:
                del _seen[jti]
        if claims["jti"] in _seen:
            raise IdTokenError("Sign-in token was already used")
        _seen[claims["jti"]] = claims["exp"]
    return claims


def verify_id_token(token, *, issuer, audience, nonce, jwks_url):
    """A sign-in token. Returns its claims ({"sub": ..., "saved"?: ...}) or raises IdTokenError (or JwksError)."""
    claims = _check(token, issuer, audience, jwks_url)
    if "events" in claims:
        raise IdTokenError("That's not a sign-in token")
    if not isinstance(nonce, str) or claims.get("nonce") != nonce:
        raise IdTokenError("Sign-in token does not match this sign-in")
    return _use(claims)


def verify_deletion_token(token, *, issuer, audience, jwks_url):
    """A notice that a 6j0 ID was deleted (the ID server POSTs it to /api/id/deleted): signed like a sign-in token,
    but with no nonce and with events: {"6j0/v1/identity-deleted": {}} (the event's name is from before "ID", and
    stays). Returns its claims or raises."""
    claims = _check(token, issuer, audience, jwks_url)
    events = claims.get("events")
    if "nonce" in claims or not isinstance(events, dict) or DELETED_EVENT not in events:
        raise IdTokenError("That's not a deletion notice")
    return _use(claims)
