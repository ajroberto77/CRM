"""server/core/trust.py -- the one definition of "trusted." No real network
call anywhere; this is pure DB + Python logic.
"""
from __future__ import annotations

import pytest

from server.core import repository, trust


class TestNormalizePattern:
    def test_a_bare_domain_gets_the_leading_at_added(self):
        assert trust.normalize_pattern("acme.com") == "@acme.com"

    def test_an_at_domain_is_lowercased_and_kept(self):
        assert trust.normalize_pattern("@ACME.com") == "@acme.com"

    def test_a_full_address_is_lowercased(self):
        assert trust.normalize_pattern("Jane@ACME.com") == "jane@acme.com"

    def test_blank_raises(self):
        with pytest.raises(ValueError):
            trust.normalize_pattern("")

    def test_a_domain_with_no_dot_raises(self):
        with pytest.raises(ValueError):
            trust.normalize_pattern("@localhost")

    def test_an_address_with_no_local_part_raises(self):
        with pytest.raises(ValueError):
            trust.normalize_pattern("@jane@acme.com")


class TestIsPublicDomain:
    def test_known_public_domain(self):
        assert trust.is_public_domain("@gmail.com") is True

    def test_private_domain(self):
        assert trust.is_public_domain("@acme.com") is False

    def test_requires_the_at_prefix(self):
        assert trust.is_public_domain("gmail.com") is False


class TestAddSender:
    def test_creates_a_new_trusted_sender(self, org_id, principal):
        trust.add_sender(principal, "@acme.com", label="Acme")
        listing = repository.list_records(principal, "trusted_sender")
        assert listing["total"] == 1
        assert listing["records"][0]["pattern"] == "@acme.com"

    def test_re_adding_updates_rather_than_duplicates(self, org_id, principal):
        trust.add_sender(principal, "@acme.com", label="Acme")
        trust.add_sender(principal, "@acme.com", label="Acme Corp Updated")
        listing = repository.list_records(principal, "trusted_sender")
        assert listing["total"] == 1
        assert listing["records"][0]["label"] == "Acme Corp Updated"

    def test_a_bare_domain_normalizes_before_storage(self, org_id, principal):
        trust.add_sender(principal, "acme.com")
        listing = repository.list_records(principal, "trusted_sender")
        assert listing["records"][0]["pattern"] == "@acme.com"


class TestTrustReason:
    def test_blank_email_returns_none(self, org_id):
        assert trust.trust_reason(org_id, "") is None
        assert trust.trust_reason(org_id, None) is None

    def test_unrecognized_email_returns_none(self, org_id):
        assert trust.trust_reason(org_id, "nobody@nowhere.com") is None

    def test_a_domain_pattern_matches_any_address_at_that_domain(self, org_id, principal):
        trust.add_sender(principal, "@acme.com", label="Acme")
        assert trust.trust_reason(org_id, "bob@acme.com") == "sender:@acme.com"
        assert trust.trust_reason(org_id, "carol@acme.com") == "sender:@acme.com"

    def test_an_exact_address_wins_over_its_own_domain(self, org_id, principal):
        trust.add_sender(principal, "@acme.com")
        trust.add_sender(principal, "carol@acme.com")
        assert trust.trust_reason(org_id, "carol@acme.com") == "sender:carol@acme.com"
        assert trust.trust_reason(org_id, "dave@acme.com") == "sender:@acme.com"

    def test_a_trusted_contact_via_auto_accept_flag(self, org_id, principal):
        person = repository.create(principal, "person", {"full_name": "Jane Doe", "auto_accept": True})
        repository.create(principal, "contact_channel", {
            "person_id": str(person["id"]), "kind": "email",
            "value_normalized": "jane@example.com", "value_raw": "Jane@Example.com",
        })
        assert trust.trust_reason(org_id, "Jane@Example.com") == "contact:Jane Doe"

    def test_a_matching_channel_without_auto_accept_is_not_trusted(self, org_id, principal):
        person = repository.create(principal, "person", {"full_name": "Jane Doe"})
        repository.create(principal, "contact_channel", {
            "person_id": str(person["id"]), "kind": "email",
            "value_normalized": "jane@example.com", "value_raw": "jane@example.com",
        })
        assert trust.trust_reason(org_id, "jane@example.com") is None

    def test_is_trusted_matches_trust_reason(self, org_id, principal):
        trust.add_sender(principal, "@acme.com")
        assert trust.is_trusted(org_id, "bob@acme.com") is True
        assert trust.is_trusted(org_id, "bob@other.com") is False
