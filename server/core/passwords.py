"""Password hashing and token minting. Stdlib only.

Hashing is ported from Cal's dashboard_auth.py, which got the details right and
is worth keeping identical rather than reinventing (R1):

  * A self-describing hash string, so the iteration count can be raised later
    without invalidating existing hashes.
  * hmac.compare_digest for the comparison.
  * A malformed or unknown-algorithm hash returns False rather than raising --
    a corrupted row must deny access, not 500 every request that touches it.

What does NOT carry over is Cal's fail-open-when-unconfigured behavior. That
exists so a single operator cannot lock themselves out of the Settings page they
need in order to configure auth. A multi-user CRM fails closed and offers a
first-run setup path instead.

Format:  pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 240_000
_SALT_BYTES = 16
_TOKEN_BYTES = 32

MIN_PASSWORD_LENGTH = 12


class WeakPasswordError(ValueError):
    pass


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> str:
    """Returns a storable hash string with a fresh random salt."""
    if not isinstance(password, str) or not password:
        raise WeakPasswordError("a password is required")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Constant-time check of `password` against a stored hash string.

    Returns False for a null hash: a user row with no password is invited but
    not activated, and must never authenticate. Returns False rather than
    raising for a malformed hash, for the reason in the module docstring.
    """
    if not stored or not isinstance(password, str):
        return False
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        rounds = int(iterations)
    except (AttributeError, ValueError):
        return False
    if algorithm != ALGORITHM or rounds < 1 or not expected:
        return False
    return hmac.compare_digest(_derive(password, salt, rounds), expected)


def needs_rehash(stored: Optional[str]) -> bool:
    """True when a stored hash uses fewer iterations than the current cost, so
    a successful login can transparently upgrade it. This is the payoff for the
    self-describing format."""
    if not stored:
        return False
    try:
        algorithm, iterations, _, _ = stored.split("$")
        return algorithm != ALGORITHM or int(iterations) < ITERATIONS
    except (AttributeError, ValueError):
        # Unparseable -- it cannot verify anyway, so there is nothing to upgrade.
        return False


def check_password_strength(password: str) -> None:
    """Raise WeakPasswordError if the password is unacceptable.

    Deliberately a length floor and nothing else. Composition rules (a digit, a
    symbol, mixed case) measurably push people toward predictable substitutions
    and are not worth the friction; length is the property that matters.
    """
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )


# ── Session tokens ───────────────────────────────────────────────────────────

def new_session_token() -> str:
    """A fresh opaque bearer token. URL-safe so it can live in a cookie or an
    Authorization header without encoding."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    """The stored form of a session token.

    Plain SHA-256, not PBKDF2, and deliberately: a 256-bit random token has no
    guessable structure, so key stretching buys nothing, while it would cost a
    240k-iteration derivation on every authenticated request. What matters is
    that the database never holds a usable token -- a disclosure must not hand
    over live sessions.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
