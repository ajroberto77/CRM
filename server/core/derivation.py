"""Person derivation from the interaction log, and promotion of a derived
record back to human-owned the moment a human edits it -- M2's "importing a
mailbox materializes people nobody typed" (docs/DESIGN.md).

Core, not a module: `organization`/`person`'s `is_derived`/`source` columns
and `interaction`/`contact_channel` are all core primitives (there is no
vertical vocabulary here, so R6 does not apply). Wired in from
`server/api/app.py`'s startup alongside `registry.register_core_entities()`,
the same way `modules/investor_portal`'s subscriber wires in from its own
`install()`.
"""
from __future__ import annotations

from typing import Optional

from server.core import events, identity, permissions, registry, repository

# interaction.kind -> the contact_channel kind its from_channel/to_channels
# values actually are. Only kinds with an unambiguous mapping derive at all;
# 'meeting'/'chat'/'other' carry no reliable channel shape and are skipped
# rather than guessed at.
_CHANNEL_KIND_FOR_INTERACTION_KIND = {
    "email": "email",
    "call": "phone",
    "sms": "phone",
}


def resolve_or_create_person(principal, channel_kind: str, raw_value: str) -> Optional[str]:
    """Look up an existing contact_channel for `raw_value`; materialize a
    person + channel only if none matches. Returns the resolved or newly
    created person's id, or None for an unparseable value -- there is no
    value to fabricate a person around. Public: also called directly by
    `server/jobs/sync_poller.py` (M4), which needs the person id to record
    a `core.sync_links` mapping back to the external contact.

    Known gap: this is select-then-insert, not atomic. Two callers naming
    the same brand-new contact for the first time, processed concurrently,
    can each pass the "no existing channel" check before either commits,
    producing two derived persons instead of one (the second's
    contact_channel insert loses the race against
    `uq_contact_channels_org_kind_value`, so it fails rather than silently
    duplicating the channel -- but the extra person row stands).
    `associations.associate()` solves the same class of race with
    `INSERT ... ON CONFLICT DO NOTHING RETURNING *`, which requires hand-
    written SQL against the repository's one choke point; there is no
    generic atomic get-or-create primitive to call instead yet. Narrow and
    low-severity (a duplicate derived record, not data loss or a leak), left
    as a follow-up rather than adding hand-rolled SQL here for it.
    """
    normalized = identity.normalize(channel_kind, raw_value)
    if normalized is None:
        return None

    existing = repository.list_records(
        principal, "contact_channel",
        filters={"and": [
            {"field": "kind", "op": "eq", "value": channel_kind},
            {"field": "value_normalized", "op": "eq", "value": normalized},
        ]},
        limit=1,
    )
    if existing["total"] > 0:
        return str(existing["records"][0]["person_id"])

    person = repository.create(principal, "person", {
        # Best available label until a human promotes the record with a
        # real name -- the raw channel value, not the normalized one
        # (identity.py: never show a user the normalized form).
        "full_name": raw_value,
        "source": "derived",
        "is_derived": True,
    })
    repository.create(principal, "contact_channel", {
        "person_id": str(person["id"]),
        "kind": channel_kind,
        "value_normalized": normalized,
        "value_raw": raw_value,
    })
    return str(person["id"])


def _derive_contacts(event: events.RecordEvent) -> None:
    """On a new interaction, resolve (or materialize) a person for every
    channel value it names. Idempotent: re-processing the same interaction
    (or a second interaction from the same channel) never creates a second
    person, because the contact_channel lookup above is the same query
    every time."""
    interaction = event.after
    if not interaction:
        return
    channel_kind = _CHANNEL_KIND_FOR_INTERACTION_KIND.get(interaction.get("kind"))
    if channel_kind is None:
        return

    principal = permissions.system_principal(event.org_id, "derive contact from interaction")
    raw_values = [interaction.get("from_channel")] + list(interaction.get("to_channels") or [])
    for raw in raw_values:
        if raw:
            resolve_or_create_person(principal, channel_kind, raw)


def _promote_on_human_edit(event: events.RecordEvent) -> None:
    """A derived record is promoted -- `is_derived` cleared -- the moment a
    human edits it, on any field. A system-originated write (this
    subscriber's own promotion update included) never re-triggers itself:
    `actor_kind` is `'system:<reason>'` only for `permissions.system_principal()`
    writes, never for a real user's.
    """
    if event.actor_kind.startswith("system"):
        return
    if not event.before.get("is_derived"):
        return
    if not event.after.get("is_derived"):
        return  # this same write already cleared it
    principal = permissions.system_principal(
        event.org_id, "promote derived record on human edit"
    )
    repository.update(principal, event.entity, str(event.record_id), {"is_derived": False})


def install() -> None:
    """Wire in the derivation subscriber and the promotion rule for both
    entities that carry `is_derived`. Idempotent, same as every other
    registration function in this platform."""
    events.subscribe(
        "core.derive_contacts_from_interaction", _derive_contacts,
        entities=("interaction",), actions=(events.CREATED,),
    )
    for entity_name in ("person", "organization"):
        events.subscribe(
            f"core.promote_{entity_name}_on_human_edit", _promote_on_human_edit,
            entities=(entity_name,), actions=(events.UPDATED,),
        )
