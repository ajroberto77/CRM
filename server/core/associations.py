"""Dated, role-bearing relationships.

This one table is why there is no `investors` table, no `portfolio_companies`
table, no `employments` table and no `co_investors` table. One legal entity is
routinely several of those at once, and separate tables would split it into
duplicates with separate histories — see `docs/VERTICAL-ASSET-MANAGEMENT.md`.

## One row, read from both ends

A relationship is stored once. Storing both directions doubles every write, and
the copies drift the first time one is edited through a path that does not know
about the mirror — which is also a second write path, the thing R4 exists to
prevent.

Direction is presentation: `works_at` reads as "employs" from the other side,
and that inverse label lives on the role declaration, not in a stored row.
`core.association_edges` unions the table with itself so either endpoint can be
queried, excluding self-loops from the second branch so a person-to-person link
is not counted twice.

## Associations are not a permission subject

There is no `owner_id` here, so `visibility_predicate()` is never called with
`'association'` — it would emit a predicate on a column that does not exist.
Access is derived instead: an edge is readable when both endpoints are, and
writable when the principal can edit the near side and read the far side.

Hydration goes through `repository.fetch_many()` rather than a SQL join, so a
related record the principal cannot see is simply absent. A join would render
the names of records the visibility level was meant to hide.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any, Iterable, Optional

from server.core import hierarchy, permissions, registry, repository
from server.core.permissions import Principal
from server.core.registry import UnknownField
from server.db import pool


class AssociationError(ValueError):
    pass


def _validate_endpoints(role: registry.AssociationRole, from_type: str, to_type: str) -> None:
    if from_type not in role.from_types:
        raise AssociationError(
            f"{role.name} goes from {', '.join(role.from_types)}, not {from_type}"
        )
    if to_type not in role.to_types:
        raise AssociationError(
            f"{role.name} goes to {', '.join(role.to_types)}, not {to_type}"
        )


def _canonical(
    role: registry.AssociationRole,
    from_type: str, from_id: str, to_type: str, to_id: str,
) -> tuple[str, str, str, str]:
    """Orient a symmetric relationship deterministically.

    Without this, `A co_investor_in B` and `B co_investor_in A` are two rows the
    uniqueness index cannot see as duplicates, and every co-investment is
    counted twice. Asymmetric roles keep the direction given.
    """
    if not role.symmetric:
        return from_type, from_id, to_type, to_id
    left, right = (from_type, from_id), (to_type, to_id)
    if left <= right:
        return from_type, from_id, to_type, to_id
    return to_type, to_id, from_type, from_id


def _parse_date(value: Any, label: str) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise AssociationError(f"{label}: {value!r} is not an ISO date") from exc


def associate(
    principal: Principal,
    *,
    role: str,
    from_type: str,
    from_id: str,
    to_type: str,
    to_id: str,
    attributes: Optional[dict[str, Any]] = None,
    valid_from: Any = None,
    valid_to: Any = None,
) -> dict[str, Any]:
    """Create a relationship.

    Requires edit on the near side and read on the far side: linking a person to
    a company is a change to both records' meaning, but only one of them needs
    to be yours to edit.
    """
    role_spec = registry.role(role)
    _validate_endpoints(role_spec, from_type, to_type)

    from_type, from_id, to_type, to_id = _canonical(
        role_spec, from_type, str(from_id), to_type, str(to_id)
    )

    principal.require("edit", from_type)
    principal.require("read", to_type)
    # Both endpoints must actually be visible, or an edge becomes a way to
    # confirm that a hidden record exists.
    repository.get_record(principal, from_type, from_id)
    repository.get_record(principal, to_type, to_id)

    starts = _parse_date(valid_from, "valid_from")
    ends = _parse_date(valid_to, "valid_to")
    if starts and ends and ends < starts:
        raise AssociationError("valid_to precedes valid_from")

    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        if role_spec.hierarchical:
            _check_no_cycle(cur, principal.org_id, role, from_type, from_id, to_type, to_id)
        cur.execute(
            "INSERT INTO core.associations "
            "  (id, org_id, from_type, from_id, to_type, to_id, role, attributes, "
            "   valid_from, valid_to, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING *",
            (
                str(uuid.uuid4()), principal.org_id, from_type, from_id, to_type, to_id,
                role, json.dumps(attributes or {}, default=str), starts, ends,
                None if principal.is_system else principal.user_id,
            ),
        )
        row = cur.fetchone()
        if row is None:
            # The uniqueness index fired: this exact relationship, with this
            # start date, already exists. Idempotent rather than an error --
            # re-linking is not a mistake worth failing a request over.
            cur.execute(
                "SELECT * FROM core.associations "
                " WHERE from_type = %s AND from_id = %s AND role = %s "
                "   AND to_type = %s AND to_id = %s "
                "   AND COALESCE(valid_from, '-infinity'::date) = "
                "       COALESCE(%s, '-infinity'::date)",
                (from_type, from_id, role, to_type, to_id, starts),
            )
            row = cur.fetchone()
        return dict(row)


def _check_no_cycle(
    cur: Any, org_id: str, role: str,
    from_type: str, from_id: str, to_type: str, to_id: str,
) -> None:
    """Reject a hierarchical edge that would close a cycle.

    Child = `from_id`, parent = `to_id` (the convention `hierarchy.py` walks
    by). A self-loop is the trivial one-node cycle; anything longer is caught
    by walking UP from the new parent (`to_id`) and checking whether the new
    child (`from_id`) is already among its ancestors -- if it is, `to_id` is
    already a descendant of `from_id`, and adding this edge would close the
    loop.

    Runs on the SAME cursor already open in `associate()`'s transaction, not
    a nested `pool.transaction()` -- exactly why `hierarchy.ancestor_type_ids()`
    exists as a raw, cursor-taking primitive separate from the permission-
    checking `hierarchy.ancestors_of()` wrapper.
    """
    if from_type == to_type and from_id == to_id:
        raise AssociationError(f"{role}: cannot link a record to itself")
    ancestors = hierarchy.ancestor_type_ids(cur, org_id, role, to_type, to_id, hierarchy.MAX_DEPTH)
    if any(a["entity_type"] == from_type and str(a["entity_id"]) == from_id for a in ancestors):
        raise AssociationError(
            f"{role}: linking would create a cycle -- {to_type} {to_id} is "
            f"already a descendant of {from_type} {from_id}"
        )


def dissociate(principal: Principal, association_id: str) -> bool:
    """Delete a relationship outright. To end one while keeping the history,
    set `valid_to` with `end_association()` instead."""
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        cur.execute(
            "SELECT * FROM core.associations WHERE id = %s", (association_id,)
        )
        row = cur.fetchone()
        if row is None:
            raise repository.NotFound(f"no association {association_id}")
        principal.require("edit", row["from_type"])
        cur.execute("DELETE FROM core.associations WHERE id = %s", (association_id,))
        return cur.rowcount > 0


def end_association(principal: Principal, association_id: str, on: Any = None) -> dict[str, Any]:
    """Close a relationship as of a date, preserving it in history. This is what
    makes "was CFO until March" answerable rather than destroyed."""
    ends = _parse_date(on, "valid_to") or date.today()
    with pool.transaction(org_id=principal.org_id, user_id=principal.user_id) as cur:
        cur.execute("SELECT * FROM core.associations WHERE id = %s", (association_id,))
        row = cur.fetchone()
        if row is None:
            raise repository.NotFound(f"no association {association_id}")
        principal.require("edit", row["from_type"])
        cur.execute(
            "UPDATE core.associations SET valid_to = %s, updated_at = now() "
            "WHERE id = %s RETURNING *",
            (ends, association_id),
        )
        return dict(cur.fetchone())


def _as_of_clause(as_of: Optional[date], include_history: bool) -> tuple[str, list[Any]]:
    if include_history:
        return "TRUE", []
    when = as_of or date.today()
    return (
        "(valid_from IS NULL OR valid_from <= %s) "
        "AND (valid_to IS NULL OR valid_to >= %s)",
        [when, when],
    )


def edges_for(
    principal: Principal,
    subject_type: str,
    subject_id: str,
    *,
    roles: Optional[Iterable[str]] = None,
    as_of: Any = None,
    include_history: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Every relationship touching a record, from either end, in ONE query.

    A record page with four related blocks is one round trip, not four — and not
    one per related row, which is the shape this replaces.
    """
    principal.require("read", subject_type)
    when, when_params = _as_of_clause(_parse_date(as_of, "as_of"), include_history)

    sql = (
        "SELECT id, self_type, self_id, other_type, other_id, role, direction, "
        "       attributes, valid_from, valid_to "
        "  FROM core.association_edges "
        " WHERE self_type = %s AND self_id = %s AND (" + when + ")"
    )
    params: list[Any] = [subject_type, str(subject_id)] + when_params
    if roles:
        role_list = list(roles)
        for name in role_list:
            registry.role(name)  # raises on an unknown role rather than matching nothing
        sql += " AND role = ANY(%s)"
        params.append(role_list)
    sql += " ORDER BY valid_to NULLS FIRST, valid_from DESC NULLS LAST LIMIT %s"
    params.append(int(limit))

    with pool.transaction(
        org_id=principal.org_id, user_id=principal.user_id, readonly=True
    ) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def related_blocks(
    principal: Principal,
    subject_type: str,
    subject_id: str,
    *,
    roles: Optional[Iterable[str]] = None,
    as_of: Any = None,
    include_history: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Relationships grouped for a record page, with the far side hydrated.

    Targets are fetched in one batch per entity type through
    `repository.fetch_many`, so this is O(entity types) queries rather than
    O(related rows) — and rows the principal cannot read simply do not appear.
    Rendering "3 hidden" instead would leak their existence.
    """
    edges = edges_for(
        principal, subject_type, subject_id,
        roles=roles, as_of=as_of, include_history=include_history,
    )
    if not edges:
        return {}

    by_type: dict[str, set[str]] = {}
    for edge in edges:
        by_type.setdefault(edge["other_type"], set()).add(str(edge["other_id"]))

    hydrated: dict[str, dict[str, Any]] = {}
    for entity, ids in by_type.items():
        try:
            hydrated[entity] = repository.fetch_many(principal, entity, ids)
        except permissions.PermissionDenied:
            # No read access to that entity type at all: the block is empty
            # rather than an error, exactly like a 'none' visibility level.
            hydrated[entity] = {}

    blocks: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        target = hydrated.get(edge["other_type"], {}).get(str(edge["other_id"]))
        if target is None:
            continue
        role_spec = registry.role(edge["role"])
        label = (
            role_spec.inverse_label
            if edge["direction"] == "in" and role_spec.inverse_label
            else edge["role"]
        )
        blocks.setdefault(label, []).append({
            "association_id": str(edge["id"]),
            "role": edge["role"],
            "direction": edge["direction"],
            "attributes": edge["attributes"],
            "valid_from": edge["valid_from"],
            "valid_to": edge["valid_to"],
            "entity": edge["other_type"],
            "record": target,
        })
    return blocks


def roles_of(
    principal: Principal, subject_type: str, subject_id: str, *, as_of: Any = None
) -> list[str]:
    """Every role this record currently plays.

    The question the whole model exists to answer: one organization can be an LP
    in one fund, a co-investor on a deal, and a portfolio company of another,
    all at once, on one record with one history.
    """
    edges = edges_for(principal, subject_type, subject_id, as_of=as_of)
    return sorted({e["role"] for e in edges})
