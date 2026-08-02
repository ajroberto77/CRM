"""All SQL for orgs, users, teams and roles.

Following the discipline the sibling projects proved: a small number of modules
own SQL text, everything else calls their functions and never opens a cursor.
That is what keeps a backend change to these files rather than a hunt across the
codebase.

Every function takes and returns plain dicts.

Emails are normalized through server/core/identity.py at every write and every
lookup (R5). `email` holds the normalized form and is what uniqueness and
matching use; `email_raw` preserves what was typed, for display.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from server.core import identity, passwords, registry
from server.db import pool


class UserExistsError(ValueError):
    pass


# ── Orgs ─────────────────────────────────────────────────────────────────────

def count_identities() -> int:
    """How many logins exist across the whole instance.

    This is what first-run detection actually asks -- "can anyone log in yet?"
    -- and it reads auth.identities, which sits outside the RLS boundary
    precisely so questions like this are answerable before any tenant context
    exists. Counting core.orgs would always return zero here, because a
    context-free connection correctly sees no tenant rows.
    """
    with pool.system_transaction() as cur:
        cur.execute("SELECT count(*) AS n FROM auth.identities")
        return int((cur.fetchone() or {}).get("n") or 0)


def create_org(name: str, slug: str = "") -> dict[str, Any]:
    """Create an organization.

    The id is generated here rather than by the database default, so the
    transaction can open with tenant context already set to the org it is about
    to create. That makes the INSERT *and* its RETURNING pass an unmodified
    symmetric RLS policy -- no bootstrap exception, no widened WITH CHECK.

    (RETURNING is subject to the SELECT policy, not just WITH CHECK. An insert
    that satisfies the check still fails if USING rejects reading the row back.)
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("an organization name is required")
    slug = (slug or "").strip().lower()
    org_id = str(uuid.uuid4())
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.orgs (id, name, slug) VALUES (%s, %s, %s) RETURNING *",
            (org_id, name, slug),
        )
        org = dict(cur.fetchone())

    # After commit, not inside the org's own transaction: a seed writes
    # through the generic repository (its own transaction, its own
    # RLS-scoped connection), the same way an event subscriber does.
    for seed in registry.org_seeds():
        seed(org_id)

    return org


def get_org(org_id: str) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute("SELECT * FROM core.orgs WHERE id = %s", (org_id,))
        row = cur.fetchone()
        return dict(row) if row else None


# ── Users ────────────────────────────────────────────────────────────────────

def create_user(
    org_id: str,
    *,
    email: str,
    name: str = "",
    password: Optional[str] = None,
    is_admin: bool = False,
    team_id: Optional[str] = None,
    status: str = "active",
) -> dict[str, Any]:
    """Create a user. `password=None` leaves the hash null, which is an invited
    user who cannot yet authenticate -- verify_password() refuses a null hash.
    """
    normalized = identity.normalize_email(email)
    if normalized is None:
        raise ValueError(f"{email!r} is not a valid email address")

    password_hash = None
    if password is not None:
        passwords.check_password_strength(password)
        password_hash = passwords.hash_password(password)

    user_id = str(uuid.uuid4())
    with pool.transaction(org_id=org_id) as cur:
        cur.execute("SELECT 1 FROM core.users WHERE email = %s", (normalized,))
        if cur.fetchone():
            raise UserExistsError(f"a user with email {normalized!r} already exists")
        cur.execute(
            "INSERT INTO core.users "
            "  (id, org_id, email, email_raw, name, password_hash, is_admin, team_id, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (
                user_id,
                org_id,
                normalized,
                (email or "").strip(),
                (name or "").strip(),
                password_hash,
                is_admin,
                team_id,
                status,
            ),
        )
        row = dict(cur.fetchone())
        # The routing entry, written in the SAME transaction as the user row so
        # the two cannot diverge. Email uniqueness is enforced twice on purpose:
        # per-org in core.users, and globally here, because one address must
        # resolve to exactly one tenant for login to be unambiguous.
        cur.execute(
            "INSERT INTO auth.identities (email, org_id, user_id) VALUES (%s, %s, %s)",
            (normalized, org_id, user_id),
        )
        return row


def get_user(org_id: str, user_id: str) -> Optional[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute("SELECT * FROM core.users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def find_user_by_email(email: str) -> Optional[dict[str, Any]]:
    """Resolve a login email to its full user row.

    Two steps, and the split is the whole point of the auth schema:

      1. auth.identities, outside RLS, answers "which tenant does this address
         belong to?" -- the minimum needed to establish context. It holds no
         password hash and no profile.
      2. core.users, under RLS with that context now set, supplies everything
         else including the hash.

    So the cross-tenant surface is exactly one email-to-tenant mapping, not
    read access to every user in the instance.
    """
    normalized = identity.normalize_email(email)
    if normalized is None:
        return None
    with pool.system_transaction() as cur:
        cur.execute(
            "SELECT org_id, user_id FROM auth.identities WHERE email = %s", (normalized,)
        )
        routing = cur.fetchone()
    if routing is None:
        return None
    return get_user(str(routing["org_id"]), str(routing["user_id"]))


def list_users(org_id: str) -> list[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute("SELECT * FROM core.users ORDER BY name, email")
        return [dict(r) for r in cur.fetchall()]


def set_password(org_id: str, user_id: str, password: str) -> None:
    passwords.check_password_strength(password)
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.users SET password_hash = %s, updated_at = now() WHERE id = %s",
            (passwords.hash_password(password), user_id),
        )


def record_login(org_id: str, user_id: str, *, rehash: Optional[str] = None) -> None:
    """Stamp last_login_at, and transparently upgrade the stored hash when the
    iteration count has been raised since it was written -- the payoff for the
    self-describing hash format."""
    with pool.transaction(org_id=org_id) as cur:
        if rehash is not None:
            cur.execute(
                "UPDATE core.users SET last_login_at = now(), password_hash = %s WHERE id = %s",
                (rehash, user_id),
            )
        else:
            cur.execute(
                "UPDATE core.users SET last_login_at = now() WHERE id = %s", (user_id,)
            )


def set_status(org_id: str, user_id: str, status: str) -> None:
    if status not in ("active", "invited", "disabled"):
        raise ValueError(f"unknown user status {status!r}")
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "UPDATE core.users SET status = %s, updated_at = now() WHERE id = %s",
            (status, user_id),
        )


# ── Teams ────────────────────────────────────────────────────────────────────

def create_team(org_id: str, name: str) -> dict[str, Any]:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.teams (org_id, name) VALUES (%s, %s) RETURNING *",
            (org_id, (name or "").strip()),
        )
        return dict(cur.fetchone())


def list_teams(org_id: str) -> list[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute("SELECT * FROM core.teams ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


# ── Roles ────────────────────────────────────────────────────────────────────

def create_role(org_id: str, name: str, description: str = "") -> dict[str, Any]:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.roles (org_id, name, description) VALUES (%s, %s, %s) "
            "RETURNING *",
            (org_id, (name or "").strip(), (description or "").strip()),
        )
        return dict(cur.fetchone())


def list_roles(org_id: str) -> list[dict[str, Any]]:
    with pool.transaction(org_id=org_id, readonly=True) as cur:
        cur.execute("SELECT * FROM core.roles ORDER BY name")
        return [dict(r) for r in cur.fetchall()]


def set_role_scope(
    org_id: str,
    role_id: str,
    obj: str,
    *,
    can_create: bool = False,
    read_level: str = "none",
    edit_level: str = "none",
    delete_level: str = "none",
) -> dict[str, Any]:
    """Upsert one (role, object) scope row. Re-setting a scope replaces it
    rather than raising -- permissions are declarative state, not an append-only
    log."""
    for level in (read_level, edit_level, delete_level):
        if level not in ("all", "team", "own", "none"):
            raise ValueError(f"unknown visibility level {level!r}")
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.role_scopes "
            "  (org_id, role_id, object, can_create, read_level, edit_level, delete_level) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, role_id, object) DO UPDATE SET "
            "  can_create = excluded.can_create, read_level = excluded.read_level, "
            "  edit_level = excluded.edit_level, delete_level = excluded.delete_level "
            "RETURNING *",
            (org_id, role_id, obj, can_create, read_level, edit_level, delete_level),
        )
        return dict(cur.fetchone())


def set_field_mask(org_id: str, role_id: str, obj: str, field: str, access: str) -> None:
    if access not in ("none", "read"):
        raise ValueError(f"unknown field access {access!r}")
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.role_field_masks (org_id, role_id, object, field, access) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, role_id, object, field) DO UPDATE SET "
            "  access = excluded.access",
            (org_id, role_id, obj, field, access),
        )


def assign_role(org_id: str, user_id: str, role_id: str) -> None:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "INSERT INTO core.user_roles (org_id, user_id, role_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (org_id, user_id, role_id) DO NOTHING",
            (org_id, user_id, role_id),
        )


def revoke_role(org_id: str, user_id: str, role_id: str) -> bool:
    with pool.transaction(org_id=org_id) as cur:
        cur.execute(
            "DELETE FROM core.user_roles WHERE user_id = %s AND role_id = %s",
            (user_id, role_id),
        )
        return cur.rowcount > 0
