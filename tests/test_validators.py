"""The write-time validator mechanism -- the gap M1's event bus could not
close, because its subscribers run after commit and cannot block a write.

Tested twice: first the generic mechanism in isolation, against throwaway
entities so these tests say nothing about any particular business rule; then
the one concrete gate that exists today (`modules/investor_portal`'s
commitment-closing rule), proving the mechanism actually works end to end
against a real cross-module composition.
"""
from __future__ import annotations

import pytest

from server.core import registry, repository


class TestGenericMechanism:
    def test_a_validator_can_block_a_create(self, principal):
        def refuse_forbidden(ctx: registry.ValidationContext) -> None:
            if "FORBIDDEN" in (ctx.after or {}).get("body", ""):
                raise repository.ValidationError("body contains a forbidden word")

        registry.register_validator("note", refuse_forbidden, actions=("create",))

        with pytest.raises(repository.ValidationError):
            repository.create(principal, "note", {"body": "this is FORBIDDEN"})

        # And the write genuinely did not happen -- not partially applied.
        assert repository.list_records(principal, "note")["total"] == 0

    def test_a_validator_can_block_an_update(self, principal):
        note = repository.create(principal, "note", {"body": "fine"})

        def refuse_forbidden(ctx: registry.ValidationContext) -> None:
            if "FORBIDDEN" in (ctx.after or {}).get("body", ""):
                raise repository.ValidationError("body contains a forbidden word")

        registry.register_validator("note", refuse_forbidden, actions=("update",))

        with pytest.raises(repository.ValidationError):
            repository.update(principal, "note", str(note["id"]), {"body": "now FORBIDDEN"})

        # Rolled back -- re-fetch shows the ORIGINAL body, not a half-applied one.
        reloaded = repository.get_record(principal, "note", str(note["id"]))
        assert reloaded["body"] == "fine"

    def test_a_validator_can_block_a_delete(self, principal):
        note = repository.create(principal, "note", {"body": "protected"})

        def refuse_deleting_protected(ctx: registry.ValidationContext) -> None:
            if (ctx.before or {}).get("body") == "protected":
                raise repository.ValidationError("this note is protected")

        registry.register_validator("note", refuse_deleting_protected, actions=("delete",))

        with pytest.raises(repository.ValidationError):
            repository.delete(principal, "note", str(note["id"]))

        assert repository.get_record(principal, "note", str(note["id"]))

    def test_a_validator_scoped_to_one_action_does_not_fire_on_others(self, principal):
        calls = []
        registry.register_validator(
            "note", lambda ctx: calls.append(ctx.action), actions=("delete",)
        )

        note = repository.create(principal, "note", {"body": "x"})
        repository.update(principal, "note", str(note["id"]), {"body": "y"})
        assert calls == []  # create and update never touched a delete-only validator

        repository.delete(principal, "note", str(note["id"]))
        assert calls == ["delete"]

    def test_before_and_after_are_the_real_written_rows(self, principal):
        seen = {}

        def capture(ctx: registry.ValidationContext) -> None:
            seen["before"] = ctx.before
            seen["after"] = ctx.after
            seen["entity"] = ctx.entity
            seen["principal"] = ctx.principal

        registry.register_validator("task", capture, actions=("update",))

        task = repository.create(principal, "task", {"title": "Call Robin"})
        repository.update(principal, "task", str(task["id"]), {"status": "done"})

        assert seen["before"]["status"] == "open"
        assert seen["after"]["status"] == "done"
        # `after` is the REAL row -- normalized/defaulted fields included, not
        # a hand-rolled guess at what the write would produce.
        assert seen["after"]["id"] == task["id"]
        assert seen["entity"] == "task"
        assert seen["principal"].user_id == principal.user_id

    def test_before_is_none_on_create_and_after_is_none_on_delete(self, principal):
        seen = []
        registry.register_validator("note", lambda ctx: seen.append((ctx.before, ctx.after)))

        note = repository.create(principal, "note", {"body": "x"})
        repository.delete(principal, "note", str(note["id"]))

        create_before, create_after = seen[0]
        assert create_before is None
        assert create_after is not None

        delete_before, delete_after = seen[-1]
        assert delete_before is not None
        assert delete_after is None

    def test_multiple_validators_run_in_order(self, principal):
        calls = []
        registry.register_validator("note", lambda ctx: calls.append("second"), order=200)
        registry.register_validator("note", lambda ctx: calls.append("first"), order=100)

        repository.create(principal, "note", {"body": "x"})
        assert calls == ["first", "second"]

    def test_a_validator_on_an_unrelated_entity_never_fires(self, principal):
        calls = []
        registry.register_validator("task", lambda ctx: calls.append(1))
        repository.create(principal, "note", {"body": "x"})
        assert calls == []

    def test_no_op_update_still_runs_validators(self, principal):
        """Unlike the event bus, which skips a no-op diff entirely, a
        validator must still see every write attempt -- an empty diff is not
        the same as 'nothing happened' from a business-rule standpoint."""
        calls = []
        registry.register_validator("note", lambda ctx: calls.append(1), actions=("update",))
        note = repository.create(principal, "note", {"body": "same"})
        repository.update(principal, "note", str(note["id"]), {"body": "same"})
        assert calls == [1]

    def test_verify_catches_a_validator_registered_against_an_unknown_entity(self):
        from server.db import schema

        registry.register_validator("nonexistent_entity", lambda ctx: None)
        problems = registry.verify(schema.all_tables())
        assert any(p.get("entity") == "nonexistent_entity" for p in problems)


class TestCommitmentClosingGate:
    """The concrete case: modules/investor_portal's rule that a commitment
    cannot close without an executed subscription agreement. This is the
    thing docs/INVESTOR-PORTAL.md named as the reason the generic mechanism
    above needed to exist."""

    @pytest.fixture
    def commitment(self, principal):
        fund = repository.create(principal, "fund", {"name": "Northgate Fund II"})
        org = repository.create(principal, "organization", {"name": "Brightline Capital"})
        return repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_org_id": str(org["id"]),
             "amount": 1_000_000, "status": "signed"},
        )

    def _executed_document(self, principal, commitment, *, kind="subscription_agreement",
                           status="executed"):
        return repository.create(
            principal, "document",
            {"subject_type": "commitment", "subject_id": str(commitment["id"]),
             "kind": kind, "filename": "subscription.pdf", "status": status},
        )

    def test_closing_without_any_document_is_refused(self, principal, commitment):
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "commitment", str(commitment["id"]),
                              {"status": "closed"})
        reloaded = repository.get_record(principal, "commitment", str(commitment["id"]))
        assert reloaded["status"] != "closed", "the transaction must have rolled back"

    def test_closing_with_a_draft_document_is_still_refused(self, principal, commitment):
        self._executed_document(principal, commitment, status="draft")
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "commitment", str(commitment["id"]),
                              {"status": "closed"})

    def test_wrong_kind_does_not_satisfy_the_gate(self, principal, commitment):
        """An executed document of the WRONG kind (a side letter, say) must
        not satisfy a rule specifically about the subscription agreement."""
        self._executed_document(principal, commitment, kind="side_letter", status="executed")
        with pytest.raises(repository.ValidationError):
            repository.update(principal, "commitment", str(commitment["id"]),
                              {"status": "closed"})

    def test_wrong_subject_does_not_satisfy_the_gate(self, principal, commitment):
        """An executed subscription agreement belonging to a DIFFERENT
        commitment must not satisfy this one's gate."""
        other_fund = repository.create(principal, "fund", {"name": "Other Fund"})
        other_org = repository.create(principal, "organization", {"name": "Other Org"})
        other_commitment = repository.create(
            principal, "commitment",
            {"fund_id": str(other_fund["id"]), "investor_org_id": str(other_org["id"])},
        )
        self._executed_document(principal, other_commitment)

        with pytest.raises(repository.ValidationError):
            repository.update(principal, "commitment", str(commitment["id"]),
                              {"status": "closed"})

    def test_closing_with_an_executed_subscription_agreement_succeeds(
        self, principal, commitment
    ):
        self._executed_document(principal, commitment)
        closed = repository.update(
            principal, "commitment", str(commitment["id"]), {"status": "closed"}
        )
        assert closed["status"] == "closed"

    def test_the_gate_only_fires_on_the_transition_into_closed(self, principal, commitment):
        """An unrelated edit to an already-open commitment, or a commitment
        already closed being touched again, must not re-trigger the check."""
        # Unrelated field change on an open commitment -- no document exists,
        # and this must succeed because status never becomes 'closed'.
        updated = repository.update(
            principal, "commitment", str(commitment["id"]), {"amount": 2_000_000}
        )
        assert updated["status"] != "closed"

    def test_editing_an_already_closed_commitment_does_not_recheck(self, principal, commitment):
        self._executed_document(principal, commitment)
        repository.update(principal, "commitment", str(commitment["id"]), {"status": "closed"})

        # Now edit something unrelated on the already-closed commitment. Even
        # though the "document" concept isn't re-verified, this must not
        # raise -- the gate only guards the MOMENT of closing, not every
        # subsequent edit to a closed record.
        updated = repository.update(
            principal, "commitment", str(commitment["id"]), {"amount": 3_000_000}
        )
        assert updated["status"] == "closed"
