"""Resolves an inbound Signal/Telegram sender to a CRM user (M6's
messaging axis, `server/channels/dispatch.py`) -- distinct from
`core.contact_channels`, which maps a channel to a third party's
`person`, never to a login.

Deliberately NOT a registered entity (R4 doesn't apply) -- same reasoning
as `server/core/accounts.py`'s `connected_accounts`: this is
identity/auth-adjacent, not generic CRM data a broad edit scope should be
able to reassign. Every write here is scoped to the linking user's own
`user_id`; `server/api/channels.py` never accepts a target `user_id` from
the request body, so there is no reassignment/impersonation surface to
guard with a field-level `write_level` the way `core/registry.py` does
for `owner_id`.
"""
from __future__ import annotations

from typing import Any, Optional

from server.core import identity
from server.db import pool


class AlreadyLinkedError(RuntimeError):
    """Raised when the normalized value is already linked to a DIFFERENT
    user in this org. Never silently reassigned -- that would let one
    user hijack an identity another user already linked."""


def link_channel(org_id: str, user_id: str, kind: str, raw_value: str) -> dict[str, Any]:
    """Idempotent: re-linking the same value to the same user returns the
    existing row rather than erroring. Raises `identity.NormalizationError`
    (a ValueError) for a value that doesn't normalize for `kind`, and
    `AlreadyLinkedError` if it's already claimed by someone else."""
    normalized = identity.normalize_required(kind, raw_value)
    with pool.transaction(org_id=org_id, user_id=user_id) as cur:
        cur.execute(
            "SELECT * FROM core.user_channels WHERE org_id = %s AND kind = %s "
            "AND value_normalized = %s",
            (org_id, kind, normalized),
        )
        existing = cur.fetchone()
        if existing is not None:
            if str(existing["user_id"]) != str(user_id):
                raise AlreadyLinkedError(
                    f"this {kind} identity is already linked to another user"
                )
            return dict(existing)
        cur.execute(
            "INSERT INTO core.user_channels (org_id, user_id, kind, value_normalized, value_raw) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (org_id, user_id, kind, normalized, raw_value),
        )
        return dict(cur.fetchone())


def list_channels(org_id: str, *, user_id: Optional[str] = None) -> list[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        if user_id is not None:
            cur.execute(
                "SELECT * FROM core.user_channels WHERE org_id = %s AND user_id = %s "
                "ORDER BY created_at",
                (org_id, user_id),
            )
        else:
            cur.execute(
                "SELECT * FROM core.user_channels WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            )
        return [dict(row) for row in cur.fetchall()]


def get_channel(org_id: str, channel_id: str) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT * FROM core.user_channels WHERE org_id = %s AND id = %s",
            (org_id, channel_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def unlink_channel(org_id: str, channel_id: str) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM core.user_channels WHERE org_id = %s AND id = %s",
            (org_id, channel_id),
        )


def resolve_user(org_id: str, kind: str, raw_value: str) -> Optional[str]:
    """The reverse lookup `dispatch.receive_commands()` needs: an inbound
    message's raw sender identity -> the user_id it's linked to, or None
    for an unrecognized sender. Never guessed at -- an unresolved sender
    is simply not a command, the same fail-safe-to-nothing shape safety
    rule 8 applies to a null category."""
    normalized = identity.normalize(kind, raw_value)
    if normalized is None:
        return None
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT user_id FROM core.user_channels WHERE org_id = %s AND kind = %s "
            "AND value_normalized = %s",
            (org_id, kind, normalized),
        )
        row = cur.fetchone()
        return str(row["user_id"]) if row else None


def resolve_user_any_org(kind: str, raw_value: str) -> Optional[tuple[str, str]]:
    """Same reverse lookup as `resolve_user()`, for a caller that has no
    org context yet -- `dispatch.receive_commands()`'s Signal/Telegram
    polls are deployment-wide (one bot number/token, not one per org, per
    `telegram.py`'s own docstring), so the inbound sender has to be
    checked against every org's channels, not one already-chosen org's.

    `core.user_channels` is RLS-scoped like every core table (`FORCE ROW
    LEVEL SECURITY`), so a query with no tenant GUC set sees zero rows
    regardless of what's actually stored -- there is no single query that
    searches it across orgs. This tries each org in turn
    (`jobs.all_org_ids()`, the same cross-org discovery `sync_poller.py`
    and `message_poller.py` already use), returning the first match.
    Org counts on one self-hosted deployment are small enough that this is
    a real cost, never a correctness concern, and a value that happens to
    be linked in two different orgs (each org's own `user_channels` row is
    independently unique) resolves to whichever org's `all_org_ids()`
    happens to return first -- the same "first linked wins" shape
    `dispatch.send()` already accepts for a user with two linked channels.

    Returns `(org_id, user_id)`, or `None` if no org recognizes this
    sender at all."""
    from server import jobs  # local import: avoids a load-order cycle with server.jobs

    for org_id in jobs.all_org_ids():
        user_id = resolve_user(org_id, kind, raw_value)
        if user_id is not None:
            return org_id, user_id
    return None
