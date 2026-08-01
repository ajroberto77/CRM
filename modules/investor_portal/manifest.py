"""The investor-portal module's write-time rules.

Registers one validator: **a commitment cannot close without an executed
subscription agreement.** This is the concrete case
`docs/INVESTOR-PORTAL.md` names for the write-time validator mechanism in
`server/core/registry.py`/`server/core/repository.py` -- the gate M1's
event bus could not provide, because its subscribers run after commit and
cannot block a write.

`commitment` is a `modules/funds` entity; `document` is core. This module
depends on both, in the same one-way direction already established for
`pathway_vehicles` -> `core.funds` -- neither `server/core/` nor
`modules/funds` gains any awareness that `investor_portal` exists.
"""
from __future__ import annotations

from server.core import registry, repository

MODULE = "investor_portal"

# The `kind` this module uses on core.documents to mean "the document that,
# once executed, satisfies a commitment's closing gate." Free text at the
# schema level (documents.kind has no CHECK constraint -- it is generic); this
# constant is the one place that vocabulary is spelled, so it is never typo'd
# differently in two places (R1).
SUBSCRIPTION_AGREEMENT_KIND = "subscription_agreement"


class SubscriptionAgreementMissing(repository.ValidationError):
    """Raised when a commitment tries to close without an executed
    subscription agreement on file. A ValidationError subclass so it is
    caught by anything already handling that class, while still being
    identifiable to a caller that wants to react specifically."""


def _validate_commitment_closing(ctx: registry.ValidationContext) -> None:
    """The gate. Fires only on the transition INTO 'closed' -- a commitment
    that is already closed being edited for an unrelated reason (a note, a
    currency correction) does not re-check, and a commitment moving between
    any other two statuses is untouched by this rule entirely.
    """
    if ctx.action != "update":
        return
    before_status = (ctx.before or {}).get("status")
    after_status = (ctx.after or {}).get("status")
    if after_status != "closed" or before_status == "closed":
        return

    result = repository.list_records(
        ctx.principal, "document",
        filters={
            "and": [
                {"field": "subject_type", "op": "eq", "value": "commitment"},
                {"field": "subject_id", "op": "eq", "value": ctx.record_id},
                {"field": "kind", "op": "eq", "value": SUBSCRIPTION_AGREEMENT_KIND},
                {"field": "status", "op": "eq", "value": "executed"},
            ]
        },
        limit=1,
    )
    if result["total"] == 0:
        raise SubscriptionAgreementMissing(
            "this commitment cannot close: no executed subscription agreement "
            "is on file for it"
        )


def install() -> None:
    registry.register_validator(
        "commitment", _validate_commitment_closing,
        actions=("update",), module=MODULE,
    )
