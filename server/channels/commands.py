"""The command grammar for M6's messaging axis -- minimal, exact-match
only, reusing `server/core/proposals.py`'s approval queue rather than
inventing a second one.

Designed fresh for this platform rather than ported from JA's
`reply_loop.py`: JA's vocabulary (forward-to-attorney, edit-and-send a
free-text reply) is domain-specific to its single co-parenting reply
queue and doesn't transfer. What DOES transfer, verbatim, are two hard
safety invariants JA's own incident history established:

1. **A quote that does not resolve stops cold.** This platform's
   `proposed_change` queue is closer to JA's own calendar-proposal
   handling (quote-only, no most-recent-pending fallback) than to JA's
   single reply queue (which does have one) -- a CRM has many concurrent
   pending proposals across many subjects and users, so guessing "the
   most recent one" would routinely resolve the wrong item for the wrong
   person. JA's own reply-queue incident (a stale, unrelated pending item
   silently actioned because a quote that didn't resolve fell back to
   "most recent" instead of stopping) is exactly the failure mode a
   fallback here would risk -- it is not ported at all. A command with no
   quote, or a quote that doesn't resolve to a pending proposal this
   sender owns, is simply not a recognized command.
2. **No LLM anywhere in this path** (safety rule 1). `dispatch_command`
   below is pure exact-match code end to end -- the only "judgment" it
   exercises is a membership test against two fixed word sets.
"""
from __future__ import annotations

from typing import Optional

from server.core import proposals

_APPROVE_WORDS = {"yes", "y", "approve", "ok", "okay"}
_DECLINE_WORDS = {"no", "n", "decline", "cancel", "skip"}


def dispatch_command(org_id: str, user_id: str, text: str, quoted_id: Optional[str]) -> bool:
    """One inbound command's raw text plus the id of the message it swipe-
    quoted, if any. Returns True if it resolved to a real decision, False
    if it was not a recognized command (ignored, not an error -- ordinary
    conversational messages arrive on the same channel and are expected
    to fall through here).

    The target proposal is found ONLY by `quoted_id` ->
    `proposed_changes.notification_message_id` -> that row. There is
    deliberately no "most recent pending" fallback (see module
    docstring): no quote at all, a quote that doesn't resolve to any
    proposal, a proposal that isn't `user_id`'s own, or one that's
    already been decided are all treated identically -- not a recognized
    command.
    """
    normalized = text.strip().lower()
    if normalized in _APPROVE_WORDS:
        decide = proposals.approve
    elif normalized in _DECLINE_WORDS:
        decide = proposals.decline
    else:
        return False

    if not quoted_id:
        return False

    proposal = proposals.find_by_notification_message_id(org_id, quoted_id)
    if proposal is None:
        return False
    if str(proposal.get("owner_id") or "") != str(user_id):
        return False  # not this user's proposal to decide
    if proposal["status"] != "pending":
        return False

    try:
        decide(org_id, str(proposal["id"]), decided_by=f"channel:{user_id}")
    except proposals.AlreadyDecidedError:
        return False  # decided concurrently between the read above and here
    return True
