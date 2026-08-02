"""The one approval queue (CLAUDE.md's directory layout already names this
file), ported from Cal's `events.py` proposal lifecycle. Polymorphic over
`subject_type`/`subject_id` -- a calendar-event proposal (M5's
`server/extraction/scheduling.py`, the first and only producer today) and
any future proposal kind share this one queue rather than each inventing
its own status/decided_by/decided_at bookkeeping.

Registered as a normal R4 entity (`proposed_change`) so the generic
list/detail UI browses it for free -- but every field is writable=False
through the generic REST/repository path. Every real transition goes
through the functions here, which is the single execution choke point
safety rule 3 requires: `approve()`/`decline()`/`auto_approve()` all
funnel through `_set_decision()`, which does the actual write via
`repository.update()` with a system principal (so it still passes through
the ordinary validator/event-subscriber machinery -- there is no second,
parallel write path) and is itself the compare-and-swap claim: the
`WHERE ... status = 'pending'` the repository's own optimistic-concurrency
path already enforces means a proposal can only be decided once.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from server.core import permissions, repository

STATUSES = ("pending", "approved", "declined", "auto_approved")


class AlreadyDecidedError(RuntimeError):
    pass


def create(
    org_id: str, *, subject_type: str, subject_id: Optional[str], kind: str,
    payload: dict[str, Any], confidence: Optional[float] = None, category: str = "",
    owner_id: Optional[str] = None,
) -> dict[str, Any]:
    """`owner_id` is who should see/decide this in the generic UI's normal
    own/team visibility -- e.g. `scheduling_pipeline.py` passes the
    triggering interaction's owner, even though a system principal is what
    actually performs this write."""
    principal = permissions.system_principal(org_id, "create proposal")
    changes: dict[str, Any] = {
        "subject_type": subject_type, "subject_id": subject_id, "kind": kind,
        "payload": payload, "confidence": confidence, "category": category,
    }
    if owner_id is not None:
        changes["owner_id"] = owner_id
    return repository.create(principal, "proposed_change", changes)


def get(org_id: str, proposal_id: str) -> dict[str, Any]:
    principal = permissions.system_principal(org_id, "read proposal")
    return repository.get_record(principal, "proposed_change", proposal_id)


def set_notification_message_id(org_id: str, proposal_id: str, message_id: str) -> dict[str, Any]:
    principal = permissions.system_principal(org_id, "record proposal notification")
    return repository.update(
        principal, "proposed_change", proposal_id, {"notification_message_id": message_id}
    )


def find_by_notification_message_id(org_id: str, message_id: str) -> Optional[dict[str, Any]]:
    """The reverse lookup a channel command's swipe-quote needs (M6's
    `server/channels/commands.py`): the opaque message id a notification
    was sent as -> the proposal it's about, or `None` if it doesn't
    resolve to anything. A quote that doesn't resolve is deliberately
    the caller's problem to treat as "not a command" rather than falling
    back to guessing -- this function only ever returns an exact match or
    nothing."""
    principal = permissions.system_principal(org_id, "resolve proposal by notification id")
    listing = repository.list_records(
        principal, "proposed_change",
        filters={"field": "notification_message_id", "op": "eq", "value": message_id},
        limit=1,
    )
    if listing["total"] == 0:
        return None
    return listing["records"][0]


def _set_decision(
    org_id: str, proposal_id: str, status: str, decided_by: str, *, from_status: str = "pending"
) -> dict[str, Any]:
    current = get(org_id, proposal_id)
    if current["status"] != from_status:
        raise AlreadyDecidedError(
            f"proposal {proposal_id} is already {current['status']!r}, not {from_status!r}"
        )
    principal = permissions.system_principal(org_id, f"proposal decision: {decided_by}")
    try:
        return repository.update(
            principal, "proposed_change", proposal_id,
            {"status": status, "decided_by": decided_by, "decided_at": datetime.now(timezone.utc)},
            if_unmodified_since=current["updated_at"],
        )
    except repository.Conflict as exc:
        # Someone else's decision landed between the read above and this
        # write -- the compare-and-swap claim losing, not a bug. Surface it
        # as the same "already decided" outcome rather than a generic 409,
        # so a caller (a Signal "yes" racing a dashboard click) has one
        # error to handle.
        raise AlreadyDecidedError(f"proposal {proposal_id} was decided concurrently") from exc


def approve(org_id: str, proposal_id: str, decided_by: str = "dashboard") -> dict[str, Any]:
    return _set_decision(org_id, proposal_id, "approved", decided_by)


def decline(org_id: str, proposal_id: str, decided_by: str = "dashboard") -> dict[str, Any]:
    return _set_decision(org_id, proposal_id, "declined", decided_by)


def auto_approve(org_id: str, proposal_id: str, reason: str) -> dict[str, Any]:
    return _set_decision(org_id, proposal_id, "auto_approved", reason)


def all_decided_for(org_id: str, subject_type: str, subject_id: str) -> bool:
    principal = permissions.system_principal(org_id, "check proposal decisions")
    pending = repository.list_records(
        principal, "proposed_change",
        filters={"and": [
            {"field": "subject_type", "op": "eq", "value": subject_type},
            {"field": "subject_id", "op": "eq", "value": subject_id},
            {"field": "status", "op": "eq", "value": "pending"},
        ]},
        limit=1,
    )
    return pending["total"] == 0
