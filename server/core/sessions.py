"""All SQL for sessions, plus the login/logout decisions.

Only the token HASH is ever stored (server/core/passwords.py explains why plain
SHA-256 is the right choice for a 256-bit random token). The raw token exists
exactly once, in the response that created it.

A session is valid when it is unexpired, unrevoked, idle-within-bounds, and its
user is still active. Every one of those is checked in SQL in a single query, so
there is no window where a disabled user keeps working because the check lives
somewhere the request did not pass through.
"""
from __future__ import annotations

from typing import Any, Optional

from server import config
from server.core import passwords, users
from server.db import pool


class AuthenticationError(Exception):
    """Wrong credentials, or a session that is no longer valid. Carries no
    detail about which half failed."""


def authenticate(email: str, password: str) -> dict[str, Any]:
    """Verify credentials and return the user row.

    A missing user still runs a password verification against a dummy hash, so
    an unknown address costs the same as a wrong password and response timing
    does not reveal which addresses exist.
    """
    user = users.find_user_by_email(email)
    stored = user.get("password_hash") if user else None

    ok = passwords.verify_password(password, stored)
    if user is None:
        # Constant-ish work for a nonexistent user. The comparison result is
        # discarded; only the time it took matters.
        passwords.verify_password(password, _DUMMY_HASH)
        raise AuthenticationError("invalid email or password")
    if not ok:
        raise AuthenticationError("invalid email or password")
    if user.get("status") != "active":
        # Deliberately the same message: whether an address is disabled is not
        # something an unauthenticated caller should be able to probe.
        raise AuthenticationError("invalid email or password")

    rehash = None
    if passwords.needs_rehash(stored):
        rehash = passwords.hash_password(password)
    users.record_login(str(user["org_id"]), str(user["id"]), rehash=rehash)
    if rehash:
        user["password_hash"] = rehash
    return user


# A real hash of an unguessable value, derived once at import so the dummy
# verification costs the same as a real one.
_DUMMY_HASH = passwords.hash_password(passwords.new_session_token())


def create_session(
    org_id: str,
    user_id: str,
    *,
    user_agent: str = "",
    ip: str = "",
) -> tuple[str, dict[str, Any]]:
    """Mint a session. Returns (raw_token, session_row); the raw token is not
    recoverable afterwards."""
    token = passwords.new_session_token()
    token_hash = passwords.hash_token(token)
    ttl = config.get_session_ttl_seconds()
    with pool.transaction(org_id=org_id, user_id=user_id) as cur:
        cur.execute(
            "INSERT INTO auth.sessions (org_id, user_id, token_hash, expires_at, user_agent, ip) "
            "VALUES (%s, %s, %s, now() + (%s * interval '1 second'), %s, %s) "
            "RETURNING *",
            (org_id, user_id, token_hash, ttl, user_agent[:500], ip[:100]),
        )
        return token, dict(cur.fetchone())


def resolve_session(token: str) -> Optional[dict[str, Any]]:
    """Return `{session, user}` for a valid token, else None.

    Context-free by necessity: the token is what identifies the org, so there is
    no context to set before looking it up. This is why `uq_sessions_token` is a
    global unique index rather than org-leading.

    Validity is decided in SQL -- unexpired, unrevoked, within the idle window,
    and the user still active -- so there is no code path that can skip part of
    the check.
    """
    if not token:
        return None
    token_hash = passwords.hash_token(token)
    idle = config.get_session_idle_seconds()

    # Step 1, context-free: the token decides which tenant. auth.sessions is
    # outside RLS for exactly this reason. Token validity -- unrevoked,
    # unexpired, within the idle window -- is decided here in SQL.
    with pool.system_transaction() as cur:
        cur.execute(
            "SELECT id AS session_id, org_id, user_id, expires_at "
            "FROM auth.sessions "
            "WHERE token_hash = %s "
            "  AND revoked_at IS NULL "
            "  AND expires_at > now() "
            "  AND last_seen_at > now() - (%s * interval '1 second')",
            (token_hash, idle),
        )
        row = cur.fetchone()
        if row is None:
            return None
        session = dict(row)
        # Sliding idle window, written in the same transaction that read it so a
        # concurrent request cannot expire a session that just succeeded.
        cur.execute(
            "UPDATE auth.sessions SET last_seen_at = now() WHERE id = %s",
            (session["session_id"],),
        )

    org_id = str(session["org_id"])

    # Step 2, now under tenant context: the user record itself. The status check
    # lives here rather than in the caller, so disabling a user takes effect on
    # their next request instead of when their session happens to expire.
    with pool.transaction(org_id=org_id, user_id=str(session["user_id"])) as cur:
        cur.execute(
            "SELECT id, org_id, email, email_raw, name, is_admin, team_id, status "
            "FROM core.users WHERE id = %s AND status = 'active'",
            (str(session["user_id"]),),
        )
        user = cur.fetchone()
    if user is None:
        return None

    return {"session_id": str(session["session_id"]), "user": dict(user)}


def revoke_session(org_id: str, session_id: str) -> bool:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE auth.sessions SET revoked_at = now() "
            "WHERE id = %s AND revoked_at IS NULL",
            (session_id,),
        )
        return cur.rowcount > 0


def revoke_all_for_user(org_id: str, user_id: str) -> int:
    """Used when a password changes or a user is disabled -- otherwise existing
    sessions outlive the credential that created them."""
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE auth.sessions SET revoked_at = now() "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return cur.rowcount


def purge_expired(older_than_days: int = 30) -> int:
    """Delete long-dead session rows. Expiry is enforced by the query in
    resolve_session(), so this is housekeeping, not a security control."""
    with pool.system_transaction() as cur:
        cur.execute(
            "DELETE FROM auth.sessions "
            "WHERE expires_at < now() - (%s * interval '1 day')",
            (older_than_days,),
        )
        return cur.rowcount
