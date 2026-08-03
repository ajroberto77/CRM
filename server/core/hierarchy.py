"""Ancestor/descendant traversal for hierarchical association roles.

Generic over any `AssociationRole` marked `hierarchical=True` (registry.py)
-- an edge's `from_id` is the child, `to_id` is the parent, exactly like
every other association. Two roles use this: core's `owned_by`
(legal/corporate ownership) and modules/funds' `rolls_up_to` (the
investment-relationship rollup, which deliberately is not legal-entity-
based) -- different concepts, same traversal mechanism, hence living here
once rather than duplicated per role.

## Depth cap, and why it exists alongside the write-time cycle check

`associations.associate()` rejects a hierarchical edge that would close a
cycle (checked at write time, before the INSERT commits). This module's
`MAX_DEPTH` cap is a second, independent guard, not a redundant one: the
cycle check only runs through `associate()`, and a direct SQL write (a
migration, a manual fix, a bug elsewhere) bypasses it entirely. A recursive
CTE with no cap over a cyclic graph never terminates. Neither guard alone
is sufficient; both ship together.

No denormalized "ultimate parent" column is stored anywhere -- at this
product's scale a recursive CTE over `core.associations` is cheap, and a
maintained denormalization (keeping it in sync on every edge write/end) is
a real correctness burden for a product this size. Revisit only if this is
ever measured slow.
"""
from __future__ import annotations

from typing import Any, Optional

from server.core import registry
from server.core.permissions import Principal
from server.db import pool

MAX_DEPTH = 50

# Walks the association graph one hierarchical role at a time, org-scoped,
# current edges only (valid_to IS NULL -- a closed relationship is history,
# not part of today's structure). `%(start_side)s`/`%(next_side)s...` are
# filled in per direction by the two callers below; the shape is otherwise
# identical up/down, which is why it is written once.
_ANCESTORS_SQL = """
    WITH RECURSIVE walked AS (
        SELECT to_type AS entity_type, to_id AS entity_id, 1 AS depth
          FROM core.associations
         WHERE org_id = %(org_id)s AND role = %(role)s
           AND from_type = %(subject_type)s AND from_id = %(subject_id)s
           AND valid_to IS NULL
        UNION ALL
        SELECT a.to_type, a.to_id, w.depth + 1
          FROM core.associations a
          JOIN walked w ON a.from_type = w.entity_type AND a.from_id = w.entity_id
         WHERE a.org_id = %(org_id)s AND a.role = %(role)s AND a.valid_to IS NULL
           AND w.depth < %(max_depth)s
    )
    SELECT entity_type, entity_id, MIN(depth) AS depth
      FROM walked
     GROUP BY entity_type, entity_id
     ORDER BY depth
"""

_DESCENDANTS_SQL = """
    WITH RECURSIVE walked AS (
        SELECT from_type AS entity_type, from_id AS entity_id, 1 AS depth
          FROM core.associations
         WHERE org_id = %(org_id)s AND role = %(role)s
           AND to_type = %(subject_type)s AND to_id = %(subject_id)s
           AND valid_to IS NULL
        UNION ALL
        SELECT a.from_type, a.from_id, w.depth + 1
          FROM core.associations a
          JOIN walked w ON a.to_type = w.entity_type AND a.to_id = w.entity_id
         WHERE a.org_id = %(org_id)s AND a.role = %(role)s AND a.valid_to IS NULL
           AND w.depth < %(max_depth)s
    )
    SELECT entity_type, entity_id, MIN(depth) AS depth
      FROM walked
     GROUP BY entity_type, entity_id
     ORDER BY depth
"""


def _require_hierarchical(role: str) -> registry.AssociationRole:
    role_spec = registry.role(role)
    if not role_spec.hierarchical:
        raise ValueError(f"{role!r} is not a hierarchical association role")
    return role_spec


def ancestor_type_ids(
    cur: Any, org_id: str, role: str, subject_type: str, subject_id: str,
    max_depth: int = MAX_DEPTH,
) -> list[dict[str, Any]]:
    """Raw traversal on an already-open cursor, no permission check and no
    transaction of its own -- the shared primitive both `ancestors_of()`
    below and `associations.associate()`'s write-time cycle check use, so
    the recursive CTE exists in exactly one place (R1)."""
    cur.execute(
        _ANCESTORS_SQL,
        {
            "org_id": org_id, "role": role, "subject_type": subject_type,
            "subject_id": str(subject_id), "max_depth": max_depth,
        },
    )
    return [dict(r) for r in cur.fetchall()]


def ancestors_of(
    principal: Principal, role: str, subject_type: str, subject_id: str,
    *, max_depth: int = MAX_DEPTH,
) -> list[dict[str, Any]]:
    """Every ancestor of `subject`, nearest first, `[]` if it has none.
    Raises ValueError if `role` is not marked hierarchical."""
    _require_hierarchical(role)
    principal.require("read", subject_type)
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id, readonly=True) as cur:
        return ancestor_type_ids(cur, principal.org_id, role, subject_type, str(subject_id), max_depth)


def descendants_of(
    principal: Principal, role: str, subject_type: str, subject_id: str,
    *, max_depth: int = MAX_DEPTH,
) -> list[dict[str, Any]]:
    """Every descendant of `subject` (children, grandchildren, ...), nearest
    first, `[]` if it has none. Raises ValueError if `role` is not marked
    hierarchical."""
    _require_hierarchical(role)
    principal.require("read", subject_type)
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id, readonly=True) as cur:
        cur.execute(
            _DESCENDANTS_SQL,
            {
                "org_id": principal.org_id, "role": role, "subject_type": subject_type,
                "subject_id": str(subject_id), "max_depth": max_depth,
            },
        )
        return [dict(r) for r in cur.fetchall()]


def ultimate_parent_of(
    principal: Principal, role: str, subject_type: str, subject_id: str,
    *, max_depth: int = MAX_DEPTH,
) -> dict[str, Any]:
    """The farthest ancestor reachable within `max_depth`, or `subject`
    itself if it has no parent -- so this always returns something, never
    None, and a caller never has to special-case "no hierarchy yet"."""
    chain = ancestors_of(principal, role, subject_type, subject_id, max_depth=max_depth)
    if not chain:
        return {"entity_type": subject_type, "entity_id": str(subject_id), "depth": 0}
    return chain[-1]
