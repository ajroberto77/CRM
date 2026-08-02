"""M2: the interaction log, contact-channel identity resolution, person
derivation/promotion, and metric_facts.

Exercised through `server.core.repository` directly (like
`test_vertical_funds.py` and `test_investor_portal_m9a.py`).
"""
from __future__ import annotations

import pytest

from server.core import identity, permissions, repository, users


@pytest.fixture
def other_member(org_id):
    """A second, non-owning member -- for the body-owner-scoping tests. Not
    named `member` (already a conftest fixture) to keep both available."""
    m = users.create_user(
        org_id, email="other@example.com", name="Other Member",
        password="correct-horse-battery", is_admin=False,
    )
    role = users.create_role(org_id, "Interaction Reader")
    users.set_role_scope(org_id, str(role["id"]), "interaction", read_level="all")
    users.assign_role(org_id, str(m["id"]), str(role["id"]))
    return m


@pytest.fixture
def other_member_principal(other_member):
    return permissions.load_principal(other_member)


class TestInteractionBodyScoping:
    def test_owner_and_admin_see_the_body(self, principal, admin):
        """`principal` (from conftest) is the admin's own Principal, so the
        owner and the admin are the same user here -- both paths pass."""
        created = repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "inbound", "from_channel": "jane@example.com",
             "body": "the actual message text"},
        )
        assert created["body"] == "the actual message text"
        assert created["owner_id"] == str(admin["id"])

        fetched = repository.get_record(principal, "interaction", str(created["id"]))
        assert fetched["body"] == "the actual message text"

    def test_a_non_owning_member_in_the_same_org_gets_no_body(
        self, principal, other_member_principal
    ):
        """Safety rule 10: metadata is org-wide, body is owner-scoped. The
        other member can read the row at all (read_level='all' granted
        above) but not this one field."""
        created = repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "inbound", "from_channel": "jane@example.com",
             "body": "sensitive contents"},
        )

        seen_by_other = repository.get_record(
            other_member_principal, "interaction", str(created["id"])
        )
        assert seen_by_other["body"] is None
        # Metadata columns are still visible -- only body is scoped.
        assert seen_by_other["from_channel"] == "jane@example.com"
        assert seen_by_other["kind"] == "email"

    def test_list_records_also_scopes_the_body(self, principal, other_member_principal):
        repository.create(
            principal, "interaction",
            {"kind": "call", "direction": "outbound", "body": "call notes"},
        )
        result = repository.list_records(other_member_principal, "interaction")
        assert result["total"] == 1
        assert result["records"][0]["body"] is None


class TestContactChannels:
    def test_unique_on_kind_and_normalized_value(self, principal):
        jane = repository.create(principal, "person", {"full_name": "Jane Doe"})
        repository.create(
            principal, "contact_channel",
            {"person_id": str(jane["id"]), "kind": "email",
             "value_normalized": "jane@example.com", "value_raw": "Jane@Example.com"},
        )
        with pytest.raises(Exception):
            repository.create(
                principal, "contact_channel",
                {"person_id": str(jane["id"]), "kind": "email",
                 "value_normalized": "jane@example.com", "value_raw": "jane@example.com"},
            )

    def test_the_platform_normalizer_is_what_identity_resolution_relies_on(self):
        """Not a repository test -- confirms server/core/identity.py (the one
        normalizer, R5) is what contact_channel uniqueness actually keys on."""
        assert identity.normalize("email", "Jane@EXAMPLE.com") == "jane@example.com"
        assert identity.normalize_required("email", "Jane@EXAMPLE.com") == "jane@example.com"
        with pytest.raises(identity.NormalizationError):
            identity.normalize_required("email", "not-an-email")


class TestDerivation:
    def test_a_new_channel_materializes_a_derived_person(self, principal):
        before = repository.list_records(principal, "person")["total"]
        repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "inbound", "from_channel": "newcontact@example.com"},
        )
        after = repository.list_records(
            principal, "person",
            filters={"field": "source", "op": "eq", "value": "derived"},
        )
        assert after["total"] == before + 1
        person = after["records"][0]
        assert person["is_derived"] is True

        channel = repository.list_records(
            principal, "contact_channel",
            filters={"field": "value_normalized", "op": "eq", "value": "newcontact@example.com"},
        )
        assert channel["total"] == 1
        assert channel["records"][0]["person_id"] == str(person["id"])

    def test_a_second_interaction_from_the_same_channel_does_not_duplicate(self, principal):
        repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "inbound", "from_channel": "repeat@example.com"},
        )
        repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "outbound", "to_channels": ["repeat@example.com"]},
        )
        matches = repository.list_records(
            principal, "person",
            filters={"and": [
                {"field": "source", "op": "eq", "value": "derived"},
            ]},
        )
        # Only one derived person for the one channel, despite two interactions.
        channel_people = [
            p for p in matches["records"]
        ]
        assert len(channel_people) == 1

    def test_meeting_interactions_do_not_derive_anything(self, principal):
        """No unambiguous channel-kind mapping for 'meeting' -- see
        server/core/derivation.py. Must not guess."""
        before = repository.list_records(principal, "person")["total"]
        repository.create(
            principal, "interaction",
            {"kind": "meeting", "direction": "internal", "from_channel": "someone@example.com"},
        )
        after = repository.list_records(principal, "person")["total"]
        assert after == before

    def test_an_existing_channel_resolves_without_creating_a_new_person(self, principal):
        jane = repository.create(principal, "person", {"full_name": "Jane Doe"})
        repository.create(
            principal, "contact_channel",
            {"person_id": str(jane["id"]), "kind": "email",
             "value_normalized": "jane@example.com", "value_raw": "jane@example.com"},
        )
        before = repository.list_records(principal, "person")["total"]
        repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "inbound", "from_channel": "jane@example.com"},
        )
        after = repository.list_records(principal, "person")["total"]
        assert after == before  # resolved to Jane, no new person


class TestPromotion:
    def test_editing_a_derived_person_clears_is_derived(self, principal):
        repository.create(
            principal, "interaction",
            {"kind": "email", "direction": "inbound", "from_channel": "promote@example.com"},
        )
        derived = repository.list_records(
            principal, "person",
            filters={"field": "source", "op": "eq", "value": "derived"},
        )["records"][0]
        assert derived["is_derived"] is True

        # The promotion subscriber's own follow-up write happens after this
        # update() call's row snapshot was already taken, so it lands in the
        # DB but not in this specific return value -- re-fetch for the
        # actually-current state, same as any other post-commit side effect.
        repository.update(
            principal, "person", str(derived["id"]), {"full_name": "Promoted Person"}
        )
        refetched = repository.get_record(principal, "person", str(derived["id"]))
        assert refetched["is_derived"] is False
        assert refetched["full_name"] == "Promoted Person"

    def test_a_human_typed_person_was_never_derived(self, principal):
        person = repository.create(principal, "person", {"full_name": "Typed In"})
        assert person["is_derived"] is False
        assert person["source"] == "human"


class TestMetricFacts:
    def test_metric_fact_crud_and_filtering(self, principal):
        acme = repository.create(principal, "organization", {"name": "Acme Corp"})
        repository.create(
            principal, "metric_fact",
            {
                "subject_type": "organization", "subject_id": str(acme["id"]),
                "period_start": "2026-01-01", "period_end": "2026-03-31",
                "metric_key": "ebitda", "value_numeric": 1500000, "currency": "USD",
            },
        )
        repository.create(
            principal, "metric_fact",
            {
                "subject_type": "organization", "subject_id": str(acme["id"]),
                "period_start": "2026-01-01", "period_end": "2026-03-31",
                "metric_key": "revenue", "value_numeric": 9000000, "currency": "USD",
            },
        )
        result = repository.list_records(
            principal, "metric_fact",
            filters={"and": [
                {"field": "subject_id", "op": "eq", "value": str(acme["id"])},
                {"field": "metric_key", "op": "eq", "value": "ebitda"},
            ]},
        )
        assert result["total"] == 1
        assert float(result["records"][0]["value_numeric"]) == 1500000
