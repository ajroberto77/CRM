"""Keeps `core.deals.last_activity_at` current -- the one signal M7's
deal-rotting computation (`server/core/registry.py`'s `_deal_rotting`)
reads. Found while building that computation: the column already existed
(with a schema comment anticipating exactly this use), but nothing in
the codebase had ever written it, so every deal's rotting flag would
silently and permanently compute `False`. This closes that gap.

Two signals count as "activity" on a deal, both event-bus subscribers
(the same pattern `derivation.py` already uses -- a subscriber on a
CREATE/UPDATE, not a bespoke trigger mechanism):

1. A human edit to the deal itself. Cheap, always-correct signal: someone
   touched this deal. System-originated writes (this subscriber's own
   bookkeeping update included) never count -- same guard
   `derivation.py`'s `_promote_on_human_edit` uses, for the same reason
   (an unconditional subscriber would re-trigger itself on its own write
   and recurse until `events.MAX_DEPTH` cuts it off).
2. A task or note created against the deal (`subject_type='deal'`,
   `subject_id=<deal id>`) -- the two entities already carry a
   polymorphic subject exactly for this kind of "activity happened
   against some other record" linking.

Deliberately NOT covering interactions: `core.interactions` has no
subject_type/subject_id of its own (it relates to a person/organization
via contact_channels/associations, a heavier surface), so wiring that in
is left as a known limitation for a future pass rather than silently
assumed to already work.
"""
from __future__ import annotations

from datetime import datetime, timezone

from server.core import events, permissions, repository


def _touch_deal(org_id: str, deal_id: str) -> None:
    principal = permissions.system_principal(org_id, "deal activity")
    repository.update(
        principal, "deal", deal_id, {"last_activity_at": datetime.now(timezone.utc)}
    )


def _on_deal_edited(event: events.RecordEvent) -> None:
    if event.actor_kind.startswith("system"):
        return  # our own bookkeeping write below -- never re-triggers itself
    if not event.record_id:
        return
    _touch_deal(event.org_id, str(event.record_id))


def _on_subject_created(event: events.RecordEvent) -> None:
    record = event.after
    if not record or record.get("subject_type") != "deal" or not record.get("subject_id"):
        return
    _touch_deal(event.org_id, str(record["subject_id"]))


def install() -> None:
    events.subscribe(
        "core.touch_deal_on_edit", _on_deal_edited,
        entities=("deal",), actions=(events.UPDATED,),
    )
    for entity_name in ("task", "note"):
        events.subscribe(
            f"core.touch_deal_on_{entity_name}_created", _on_subject_created,
            entities=(entity_name,), actions=(events.CREATED,),
        )
