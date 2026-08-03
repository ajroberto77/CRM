"""Phase B: the investment vehicle layer -- `investment_account` and the
`rolls_up_to` role that lets an account (or a nested GP organization) roll up
to an Investment GP, independent of legal-entity structure.

Exercised through `server.core.repository`/`associations` directly, matching
`test_vertical_funds.py`'s own convention.
"""
from __future__ import annotations

import pytest

from server.core import associations, hierarchy, registry, repository


@pytest.fixture
def gp(principal):
    return repository.create(principal, "organization", {"name": "Meridian Capital GP"})


@pytest.fixture
def trust_entity(principal):
    """The trust's own legal entity, mirroring funds.entity_org_id."""
    return repository.create(principal, "organization", {"name": "Doe Family Trust LLC"})


@pytest.fixture
def jane(principal):
    return repository.create(principal, "person", {"full_name": "Jane Doe"})


class TestRegistration:
    def test_the_entity_is_registered_under_the_funds_module(self):
        assert "investment_account" in registry.entities()
        assert registry.entity("investment_account").module == "funds"
        assert registry.role("rolls_up_to").module == "funds"

    def test_module_entities_get_crud_with_no_new_routes(self, principal, trust_entity):
        account = repository.create(
            principal, "investment_account",
            {"name": "Doe Family Trust", "entity_org_id": str(trust_entity["id"]),
             "account_type": "trust", "domicile_country": "US", "base_currency": "USD"},
        )
        fetched = repository.get_record(principal, "investment_account", str(account["id"]))
        assert fetched["account_type"] == "trust"

        # 'onboarding', not 'active' -- reaching 'active' is gated by
        # investor_portal's account-activation validator (Phase D), which
        # is exercised on its own terms in tests/test_account_activation_gate.py.
        # This test's job is only proving the generic update() path works.
        updated = repository.update(
            principal, "investment_account", str(account["id"]), {"status": "onboarding"}
        )
        assert updated["status"] == "onboarding"

    def test_account_type_and_status_are_validated_by_the_generic_path(self, principal):
        with pytest.raises(repository.ValidationError):
            repository.create(
                principal, "investment_account",
                {"name": "X", "account_type": "invented"},
            )
        with pytest.raises(repository.ValidationError):
            repository.create(
                principal, "investment_account",
                {"name": "X", "status": "invented"},
            )

    def test_a_direct_personal_account_needs_no_legal_entity(self, principal, jane):
        """The direct-investment-from-a-person case: no entity_org_id at
        all, just a person_id."""
        account = repository.create(
            principal, "investment_account",
            {"name": "Jane Doe -- Individual", "person_id": str(jane["id"]),
             "account_type": "individual"},
        )
        assert account["entity_org_id"] is None
        assert str(account["person_id"]) == str(jane["id"])


class TestCountryValidation:
    def test_a_lowercase_code_is_normalized_the_same_way_as_organizations(self, principal):
        """Same representation as organizations/persons: uppercased on
        write, not just accepted-if-valid -- proves the generic
        FieldSpec.normalize mechanism actually fires for a module-owned
        entity, not just core's two."""
        account = repository.create(
            principal, "investment_account", {"name": "X", "domicile_country": "gb"}
        )
        assert account["domicile_country"] == "GB"

    def test_an_invalid_code_is_refused(self, principal):
        with pytest.raises(repository.ValidationError):
            repository.create(
                principal, "investment_account", {"name": "X", "domicile_country": "USA"}
            )

    def test_blank_is_allowed(self, principal):
        account = repository.create(principal, "investment_account", {"name": "X"})
        assert account["domicile_country"] == ""


class TestRollsUpTo:
    def test_an_account_rolls_up_to_its_gp(self, principal, gp, trust_entity):
        account = repository.create(
            principal, "investment_account",
            {"name": "Doe Family Trust", "entity_org_id": str(trust_entity["id"]),
             "account_type": "trust"},
        )
        associations.associate(
            principal, role="rolls_up_to", from_type="investment_account",
            from_id=str(account["id"]), to_type="organization", to_id=str(gp["id"]),
        )

        ancestors = hierarchy.ancestors_of(
            principal, "rolls_up_to", "investment_account", str(account["id"])
        )
        assert [str(a["entity_id"]) for a in ancestors] == [str(gp["id"])]

    def test_organizations_can_nest_under_a_bigger_gp_on_the_same_role(
        self, principal, gp, trust_entity
    ):
        """One traversable dimension covers both org-level GP nesting AND the
        leaf edge from an account -- not two separate roles."""
        umbrella = repository.create(principal, "organization", {"name": "Meridian Umbrella"})
        account = repository.create(
            principal, "investment_account",
            {"name": "Doe Family Trust", "entity_org_id": str(trust_entity["id"]),
             "account_type": "trust"},
        )

        associations.associate(
            principal, role="rolls_up_to", from_type="organization",
            from_id=str(gp["id"]), to_type="organization", to_id=str(umbrella["id"]),
        )
        associations.associate(
            principal, role="rolls_up_to", from_type="investment_account",
            from_id=str(account["id"]), to_type="organization", to_id=str(gp["id"]),
        )

        chain = hierarchy.ancestors_of(
            principal, "rolls_up_to", "investment_account", str(account["id"])
        )
        assert [str(c["entity_id"]) for c in chain] == [str(gp["id"]), str(umbrella["id"])]

    def test_rolls_up_to_and_owned_by_are_independent_dimensions(
        self, principal, gp, trust_entity
    ):
        """The whole point of the redesign: an org can roll up under an
        Investment GP relationship that ignores its legal-entity structure,
        while its actual legal parent (owned_by) is someone else entirely."""
        legal_parent = repository.create(principal, "organization", {"name": "Legal Parent Co"})
        associations.associate(
            principal, role="owned_by", from_type="organization",
            from_id=str(trust_entity["id"]), to_type="organization", to_id=str(legal_parent["id"]),
        )
        associations.associate(
            principal, role="rolls_up_to", from_type="organization",
            from_id=str(trust_entity["id"]), to_type="organization", to_id=str(gp["id"]),
        )

        legal_ancestors = hierarchy.ancestors_of(
            principal, "owned_by", "organization", str(trust_entity["id"])
        )
        investment_ancestors = hierarchy.ancestors_of(
            principal, "rolls_up_to", "organization", str(trust_entity["id"])
        )
        assert [str(a["entity_id"]) for a in legal_ancestors] == [str(legal_parent["id"])]
        assert [str(a["entity_id"]) for a in investment_ancestors] == [str(gp["id"])]


class TestClassificationExtendsToAccounts:
    def test_an_investor_profile_can_target_an_investment_account(
        self, principal, trust_entity
    ):
        """investor_profile/investor_mandate's subject_type/subject_id are
        already untyped polymorphic columns (no CHECK, no closed registry
        vocabulary) -- so accepting 'investment_account' costs nothing new,
        exactly as Phase A/B planned."""
        account = repository.create(
            principal, "investment_account",
            {"name": "Doe Family Trust", "entity_org_id": str(trust_entity["id"]),
             "account_type": "trust"},
        )
        profile = repository.create(
            principal, "investor_profile",
            {"subject_type": "investment_account", "subject_id": str(account["id"]),
             "status": "prospect"},
        )
        assert profile["subject_type"] == "investment_account"

        mandate = repository.create(
            principal, "investor_mandate",
            {"subject_type": "investment_account", "subject_id": str(account["id"]),
             "asset_classes": ["venture"]},
        )
        assert mandate["asset_classes"] == ["venture"]
