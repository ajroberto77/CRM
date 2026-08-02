"""SQL for connected Microsoft/Google accounts (M4) -- the data a user's
OAuth link, sync cursor, and sync provenance state lives in.

Deliberately NOT a registered entity (R4 doesn't apply, same reasoning as
`server/core/settings.py`): `account_credentials.payload` holds a refresh
token, and the generic repository's field-mask/read-level machinery is not
where a secret should ever be one misconfigured grant away from exposure.
Every access to credentials goes through this module's own functions, never
through `server/core/repository.py`.

`core.connected_accounts` itself has no user-facing secret, but stays here
rather than being registered too -- its lifecycle (pending -> active/error,
credential swap on token rotation) doesn't fit the generic create/update
path, and a bespoke settings-style page (`web/src/settings/
ConnectedAccountsPage.tsx`) is what reads it, the same shape as `core.
settings`'s own page.
"""
from __future__ import annotations

import json
import secrets
from typing import Any, Optional

from server.db import pool

PROVIDERS = ("microsoft", "google")
RESOURCES = ("contacts", "calendar")
_OAUTH_PENDING_MAX_AGE_SECONDS = 900


# ── Connected accounts ───────────────────────────────────────────────────────

def create_pending_account(org_id: str, user_id: str, provider: str) -> dict[str, Any]:
    with pool.transaction(org_id=org_id, user_id=user_id) as cur:
        cur.execute(
            "INSERT INTO core.connected_accounts (org_id, user_id, provider) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (org_id, user_id, provider) DO UPDATE SET "
            "  status = 'pending', status_detail = '', updated_at = now() "
            "RETURNING *",
            (org_id, user_id, provider),
        )
        return dict(cur.fetchone())


def get_account(org_id: str, account_id: str) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT * FROM core.connected_accounts WHERE org_id = %s AND id = %s",
            (org_id, account_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def list_accounts(org_id: str, *, user_id: Optional[str] = None) -> list[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        if user_id is not None:
            cur.execute(
                "SELECT * FROM core.connected_accounts WHERE org_id = %s AND user_id = %s "
                "ORDER BY created_at",
                (org_id, user_id),
            )
        else:
            cur.execute(
                "SELECT * FROM core.connected_accounts WHERE org_id = %s ORDER BY created_at",
                (org_id,),
            )
        return [dict(row) for row in cur.fetchall()]


def update_account_status(
    org_id: str, account_id: str, status: str, status_detail: str = ""
) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.connected_accounts SET status = %s, status_detail = %s, "
            "updated_at = now() WHERE org_id = %s AND id = %s",
            (status, status_detail, org_id, account_id),
        )


def activate_account(org_id: str, account_id: str, email_address: str) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.connected_accounts SET status = 'active', status_detail = '', "
            "email_address = %s, updated_at = now() WHERE org_id = %s AND id = %s",
            (email_address, org_id, account_id),
        )


def delete_account(org_id: str, account_id: str) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM core.connected_accounts WHERE org_id = %s AND id = %s",
            (org_id, account_id),
        )


# ── Credentials ──────────────────────────────────────────────────────────────

def save_credentials(
    org_id: str, account_id: str, credential_type: str, payload: dict[str, Any]
) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "SELECT id FROM core.account_credentials WHERE org_id = %s AND account_id = %s",
            (org_id, account_id),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE core.account_credentials SET credential_type = %s, "
                "payload = %s::jsonb, rotated_at = now() WHERE id = %s",
                (credential_type, _json(payload), row["id"]),
            )
        else:
            cur.execute(
                "INSERT INTO core.account_credentials "
                "(org_id, account_id, credential_type, payload) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                (org_id, account_id, credential_type, _json(payload)),
            )


def get_credentials(org_id: str, account_id: str) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT * FROM core.account_credentials WHERE org_id = %s AND account_id = %s",
            (org_id, account_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ── OAuth pending (PKCE state) ───────────────────────────────────────────────

def create_oauth_pending(
    org_id: str, account_id: str, provider: str, code_verifier: str
) -> str:
    state = secrets.token_urlsafe(32)
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.oauth_pending (org_id, account_id, provider, state, "
            "code_verifier) VALUES (%s, %s, %s, %s, %s)",
            (org_id, account_id, provider, state, code_verifier),
        )
    return state


def peek_oauth_pending(org_id: str, state: str) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT * FROM core.oauth_pending WHERE org_id = %s AND state = %s",
            (org_id, state),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def pop_oauth_pending(org_id: str, state: str) -> Optional[dict[str, Any]]:
    """Single-use: a state can complete a link exactly once."""
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM core.oauth_pending WHERE org_id = %s AND state = %s RETURNING *",
            (org_id, state),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def sweep_oauth_pending(
    org_id: str, max_age_seconds: int = _OAUTH_PENDING_MAX_AGE_SECONDS
) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM core.oauth_pending WHERE org_id = %s "
            "AND created_at < now() - (%s || ' seconds')::interval",
            (org_id, max_age_seconds),
        )


# ── Sync cursors ─────────────────────────────────────────────────────────────

def get_sync_cursor(org_id: str, account_id: str, resource: str) -> str:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT cursor_token FROM core.sync_cursors "
            "WHERE org_id = %s AND account_id = %s AND resource = %s",
            (org_id, account_id, resource),
        )
        row = cur.fetchone()
        return row["cursor_token"] if row else ""


def commit_sync_cursor(org_id: str, account_id: str, resource: str, cursor_token: str) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.sync_cursors (org_id, account_id, resource, cursor_token) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (org_id, account_id, resource) "
            "DO UPDATE SET cursor_token = EXCLUDED.cursor_token, updated_at = now()",
            (org_id, account_id, resource, cursor_token),
        )


# ── Sync links ───────────────────────────────────────────────────────────────

def get_sync_link(
    org_id: str, account_id: str, provider_object_id: str
) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute(
            "SELECT * FROM core.sync_links WHERE org_id = %s AND account_id = %s "
            "AND provider_object_id = %s",
            (org_id, account_id, provider_object_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def create_sync_link(
    org_id: str, account_id: str, provider_object_id: str, entity_type: str, entity_id: str
) -> dict[str, Any]:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.sync_links (org_id, account_id, provider_object_id, "
            "entity_type, entity_id) VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, account_id, provider_object_id) DO UPDATE SET "
            "entity_type = EXCLUDED.entity_type, entity_id = EXCLUDED.entity_id "
            "RETURNING *",
            (org_id, account_id, provider_object_id, entity_type, entity_id),
        )
        return dict(cur.fetchone())


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)
