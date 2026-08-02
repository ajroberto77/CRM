"""Wires M5's scheduling extraction to the approval queue and calendar
axis -- Cal's `pipeline.py`, generalized to this platform's event-bus
subscriber shape rather than a poll loop calling it directly (the same
pattern `derivation.py` already uses: a subscriber on `interaction`
CREATE, not a bespoke trigger mechanism).

Core, not a module: this is core's own axes (LLM extraction, the approval
queue, the calendar axis) talking to each other -- no vertical vocabulary,
R6 doesn't apply. Wired in from `server/core/modules.py` alongside
`derivation.install()`.

## Routing (ported from Cal's `_route_event`/`_auto_accept_reason`)

1. The master gate, `config.calendar_write_enabled()`, checked first: off
   means every proposal stays pending, no auto-anything (safety rule 9).
2. The blanket `approval_mode` setting (`manual`/`confidence`/`auto`).
   `auto` always executes; `confidence` executes when the extraction's own
   confidence meets `confidence_threshold`.
3. Independent of mode, the category+trust rule (`auto_accept_categories`
   AND `trust.trust_reason()` both match) -- additive, never subtractive:
   it can accept something the mode would otherwise leave pending, but
   can never hold back something the mode already approved.
4. Nothing above fires -> the proposal stays `pending` for a human (or,
   once M6 lands, a channel notification) to decide.

A proposal only ever executes a real calendar write against the
INTERACTION OWNER's own linked account -- this platform has no
central/per-account send-identity split like Cal's; every user's calendar
is their own. No linked, active account -> stays pending; there is
nothing to write to yet.

Extraction failures are not caught here -- `server/core/events.py`'s
dispatcher already catches any subscriber exception, records it on
`core.events.delivery_state='failed'`/`last_error`, and leaves the
triggering write itself committed regardless (events dispatch AFTER
commit). That is exactly the "needs review, retry later" bookkeeping Cal's
`pipeline.py` invented ad hoc for this same failure mode; reusing it here
is R1, not a gap.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

from server import config
from server.core import accounts, events, proposals, settings, trust
from server.extraction import scheduling
from server.providers import calendar as calendar_dispatch

_DEFAULT_CONFIDENCE_THRESHOLD = 0.85


def _acting_account(org_id: str, user_id: str) -> Optional[dict[str, Any]]:
    for account in accounts.list_accounts(org_id, user_id=user_id):
        if account["status"] == "active":
            return account
    return None


def _execute_calendar_write(org_id: str, account: dict[str, Any], event: dict[str, Any]) -> None:
    check = calendar_dispatch.check_event(org_id, account, event["date"], event.get("title"))
    start_iso = f"{event['date']}T{event.get('time') or '09:00'}:00"
    duration = event.get("duration_minutes") or 60
    end_iso = (datetime.fromisoformat(start_iso) + timedelta(minutes=duration)).isoformat()
    kwargs = {
        "title": event.get("title") or "(untitled)",
        "start_iso": start_iso,
        "end_iso": end_iso,
        "location": event.get("location") or "",
        "body_text": event.get("note") or "",
        "attendees": event.get("attendees") or None,
    }
    target = check.get("update_target")
    if target is not None:
        calendar_dispatch.update_event(org_id, account, target["id"], **kwargs)
    else:
        calendar_dispatch.create_event(org_id, account, **kwargs)


def _auto_accept_reason(
    org_id: str, proposal: dict[str, Any], sender_email: str
) -> Optional[str]:
    approval = settings.get_settings(org_id, "approval")
    allowed = approval.get("auto_accept_categories") or []
    category = proposal.get("category")
    if not category or category not in allowed:
        return None
    reason = trust.trust_reason(org_id, sender_email)
    if reason is None:
        return None
    return f"auto:rule({category},{reason})"


def _route_proposal(
    org_id: str, owner_user_id: str, sender_email: str, proposal: dict[str, Any]
) -> None:
    if not config.calendar_write_enabled():
        return  # master gate off -- stays 'pending', no action, no auto-approve

    account = _acting_account(org_id, owner_user_id)
    approval = settings.get_settings(org_id, "approval")
    mode = approval.get("approval_mode", "manual")

    def _accept(reason: str) -> None:
        if account is not None:
            _execute_calendar_write(org_id, account, proposal["payload"])
        proposals.auto_approve(org_id, str(proposal["id"]), reason)

    if mode == "auto":
        _accept("auto:mode")
        return
    if mode == "confidence":
        threshold = approval.get("confidence_threshold", _DEFAULT_CONFIDENCE_THRESHOLD)
        if (proposal.get("confidence") or 0.0) >= threshold:
            _accept("auto:mode")
            return

    reason = _auto_accept_reason(org_id, proposal, sender_email)
    if reason is not None:
        _accept(reason)
        return

    # Stays 'pending' -- a human decides via the generic UI (a channel
    # notification is M6 scope, not yet built).


def _on_interaction_created(event: events.RecordEvent) -> None:
    interaction = event.after
    if not interaction:
        return
    if interaction.get("kind") != "email" or interaction.get("direction") != "inbound":
        return  # only inbound email is a scheduling *ask*; outbound is our own confirmation
    body = interaction.get("body") or ""
    if not body.strip():
        return

    org_id = event.org_id
    owner_user_id = str(interaction["owner_id"])
    sender_email = interaction.get("from_channel") or ""

    extraction = scheduling.extract_scheduling_info(
        org_id, body, reference_date=date.today().isoformat()
    )
    if not extraction.get("is_scheduling_relevant"):
        return

    for evt in extraction.get("events", []):
        if not evt.get("date"):
            continue  # nothing to schedule against -- not enough to propose
        proposal = proposals.create(
            org_id, subject_type="interaction", subject_id=str(interaction["id"]),
            kind="calendar_event", payload=evt,
            confidence=evt.get("confidence"), category=evt.get("category") or "",
            owner_id=owner_user_id,
        )
        _route_proposal(org_id, owner_user_id, sender_email, proposal)


def install() -> None:
    events.subscribe(
        "core.route_scheduling_proposals", _on_interaction_created,
        entities=("interaction",), actions=(events.CREATED,),
    )
