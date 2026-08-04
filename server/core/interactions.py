"""Per-participant tracking for an interaction -- who was actually on an
email/call/meeting, resolved automatically by `server/core/derivation.py`
from `interactions.from_channel`/`to_channels` (the same contact_channel
lookup that already materializes a person for a new channel value; this
module just keeps the result instead of throwing it away).

`core.interaction_participants` is not a registered entity -- same
treatment as `core.document_signers`/`core.associations`: an internal
detail derived automatically, not a thing a user creates or edits directly.
This module owns its SQL, the same "a small number of modules own SQL,
everything else calls their functions" discipline
`server/core/documents.py`/`users.py` use.

The interaction itself (`core.interactions`) IS a registered entity and
goes through `server/core/repository.py` like any other record -- only the
per-participant rows live here.
"""
from __future__ import annotations

import uuid
from typing import Any

from server.core import permissions, repository
from server.core.permissions import Principal
from server.db import pool

_VALID_ROLES = ("from", "to")


def add_participant(
    principal: Principal, interaction_id: str, *, person_id: str, role: str,
) -> dict[str, Any]:
    """Idempotent: re-processing the same interaction (`derivation.py`'s own
    contract for contact resolution) must not duplicate a participant row,
    so this is `INSERT ... ON CONFLICT DO NOTHING` against the same
    uniqueness (`org_id, interaction_id, person_id, role`) the schema
    enforces, then a re-read on conflict -- the same shape
    `associations.associate()` already uses for its own idempotent insert.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
    principal.require("edit", "interaction")
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        cur.execute(
            "INSERT INTO core.interaction_participants "
            "  (id, org_id, interaction_id, person_id, role) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (org_id, interaction_id, person_id, role) DO NOTHING "
            "RETURNING *",
            (str(uuid.uuid4()), principal.org_id, interaction_id, person_id, role),
        )
        row = cur.fetchone()
        if row is not None:
            return dict(row)
        cur.execute(
            "SELECT * FROM core.interaction_participants "
            " WHERE org_id = %s AND interaction_id = %s AND person_id = %s AND role = %s",
            (principal.org_id, interaction_id, person_id, role),
        )
        return dict(cur.fetchone())


def list_participants(principal: Principal, interaction_id: str) -> list[dict[str, Any]]:
    """Scoped to interactions the principal may actually read -- joins back
    to `core.interactions` and applies its read-visibility level the same
    way `interactions_for_person()` does, rather than trusting the caller's
    `interaction_id` unconditionally (a role scoped to `own`/`team` read on
    `interaction` must not learn participant linkage for someone else's
    mailbox just because it knows an id).
    """
    principal.require("read", "interaction")
    visible_sql, visible_params = permissions.visibility_predicate(principal, "interaction", "read", alias="i")
    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        cur.execute(
            "SELECT p.* FROM core.interaction_participants p "
            "JOIN core.interactions i ON i.id = p.interaction_id "
            f"WHERE p.interaction_id = %s AND ({visible_sql}) "
            "ORDER BY p.role, p.created_at",
            [interaction_id, *visible_params],
        )
        return [dict(r) for r in cur.fetchall()]


def interactions_for_person(
    principal: Principal, person_id: str, *, limit: int = 100,
) -> list[dict[str, Any]]:
    """Every interaction this person participated in, most recent first --
    the merged timeline's own interaction source. Hydrated through
    `repository.fetch_many()`, so `body`'s owner-scoped masking
    (`FieldSpec.read_level="own"`, safety rule 10) and ordinary read
    visibility both apply exactly as they would to any other read of an
    interaction row -- an interaction the principal cannot see (an "own"-
    scoped role, someone else's mailbox) is simply absent from the result,
    the same "hidden, not listed" contract `children_of()`/`related_blocks()`
    already give an unreadable related record.
    """
    principal.require("read", "interaction")
    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        cur.execute(
            "SELECT DISTINCT interaction_id FROM core.interaction_participants "
            "WHERE person_id = %s",
            (person_id,),
        )
        ids = [str(r["interaction_id"]) for r in cur.fetchall()]
    if not ids:
        return []
    hydrated = repository.fetch_many(principal, "interaction", ids)
    rows = list(hydrated.values())
    rows.sort(key=lambda r: r["occurred_at"], reverse=True)
    return rows[:limit]
