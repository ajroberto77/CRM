"""The merged, chronological activity feed for one record -- notes, tasks
and documents (already reachable generically through
`repository.children_of()`'s FK-reference walk) plus, for a person
specifically, every interaction they participated in
(`server/core/interactions.py`'s participant table, Phase 8 -- an
interaction carries no `subject_id`/`subject_type` FK to a person the
generic `children_of()` mechanism could otherwise discover).

A read-only view composed from two already-generic sources, not a new
write path or a new table beyond what Phase 8 already added to
`server/db/schema.py`.
"""
from __future__ import annotations

from typing import Any

from server.core import interactions, repository
from server.core.permissions import Principal

# Which timestamp field each entity's own rows carry, for merging into one
# chronological order -- `interaction` uses `occurred_at` (when the event
# actually happened); everything else uses `created_at` (when the record
# was entered). One place this mapping lives, not a per-caller assumption.
_TIMESTAMP_FIELD = {"interaction": "occurred_at"}
_DEFAULT_TIMESTAMP_FIELD = "created_at"


def timeline_for(
    principal: Principal, entity: str, record_id: str, *, limit: int = 100,
) -> list[dict[str, Any]]:
    """Every note/task/document naming this record as its subject, plus --
    for a person -- every interaction they participated in, merged into one
    list sorted newest-first. Each entry carries the same `entity`/`record`
    shape `children_of()`'s own blocks already use, plus the timestamp it
    was merged on.
    """
    entries: list[dict[str, Any]] = []
    for block in repository.children_of(principal, entity, record_id):
        ts_field = _TIMESTAMP_FIELD.get(block["entity"], _DEFAULT_TIMESTAMP_FIELD)
        for record in block["records"]:
            entries.append({
                "entity": block["entity"], "record": record,
                "occurred_at": record.get(ts_field),
            })

    if entity == "person":
        for record in interactions.interactions_for_person(principal, record_id, limit=limit):
            entries.append({
                "entity": "interaction", "record": record,
                "occurred_at": record.get("occurred_at"),
            })

    entries.sort(key=lambda e: e["occurred_at"] or "", reverse=True)
    return entries[:limit]
