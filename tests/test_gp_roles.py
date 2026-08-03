"""Phase C: roles at the GP level and the account level, plus the
admin-configurable `gp_role` reference list `principal_of` draws its
(optional) functional title from.

Exercised through `server.core.repository`/`associations` directly, matching
`test_vertical_funds.py`'s own convention.
"""
from __future__ import annotations

import pytest

from server.core import associations, registry, repository, users


@pytest.fixture
def gp(principal):
    return repository.create(principal, "organization", {"name": "Meridian Capital GP"})


@pytest.fixture
def jane(principal):
    return repository.create(principal, "person", {"full_name": "Jane Doe"})


@pytest.fixture
def trust_entity(principal):
    return repository.create(principal, "organization", {"name": "Doe Family Trust LLC"})


@pytest.fixture
def trust_account(principal, trust_entity):
    return repository.create(
        principal, "investment_account",
        {"name": "Doe Family Trust", "entity_org_id": str(trust_entity["id"]),
         "account_type": "trust"},
    )


class TestGpRoleSeeding:
    def test_creating_an_org_seeds_nine_roles_all_enabled(self, org_id, principal):
        result = repository.list_records(principal, "gp_role")
        assert result["total"] == 9

        by_key = {r["key"]: r for r in result["records"]}
        assert set(by_key) == {
            "managing_partner", "general_partner", "cfo", "coo", "cio",
            "investor_relations", "analyst", "operations", "other",
        }
        assert all(r["is_enabled"] is True for r in by_key.values())

    def test_seeding_is_scoped_per_org(self, principal):
        """A second org must get its own nine rows, not see -- or duplicate
        into -- the first org's."""
        other_org = users.create_org("Other Org", "other-gp-roles-org")
        other_admin = users.create_user(
            other_org["id"], email="admin3@example.com", name="Admin Three",
            password="correct-horse-battery", is_admin=True,
        )
        from server.core import permissions

        other_principal = permissions.load_principal(other_admin)
        first = repository.list_records(principal, "gp_role")
        second = repository.list_records(other_principal, "gp_role")
        assert first["total"] == 9
        assert second["total"] == 9
        assert {r["id"] for r in first["records"]}.isdisjoint(
            {r["id"] for r in second["records"]}
        )

    def test_gp_role_is_admin_configurable_like_investor_category(self, principal):
        """R4: no hand-written CRUD -- create/update go through the generic
        path exactly like investor_category."""
        role = repository.create(
            principal, "gp_role", {"key": "custom_role", "label": "Custom Role"}
        )
        updated = repository.update(
            principal, "gp_role", str(role["id"]), {"is_enabled": False}
        )
        assert updated["is_enabled"] is False


class TestGpLevelRoles:
    def test_principal_of_carries_an_optional_functional_title(self, principal, jane, gp):
        """The title is a soft reference (attributes.gp_role_key), not a
        real FK column on core.associations -- R6."""
        edge = associations.associate(
            principal, role="principal_of", from_type="person", from_id=str(jane["id"]),
            to_type="organization", to_id=str(gp["id"]),
            attributes={"gp_role_key": "managing_partner"},
        )
        assert edge["attributes"]["gp_role_key"] == "managing_partner"

        blocks = associations.related_blocks(principal, "organization", str(gp["id"]))
        assert blocks["principals"][0]["attributes"]["gp_role_key"] == "managing_partner"

    def test_principal_of_does_not_require_a_title(self, principal, jane, gp):
        edge = associations.associate(
            principal, role="principal_of", from_type="person", from_id=str(jane["id"]),
            to_type="organization", to_id=str(gp["id"]),
        )
        assert edge["attributes"] == {}

    def test_beneficial_ownership_is_owned_by_not_a_separate_role(self, principal, jane, gp):
        """No `beneficial_owner_of` role -- core's `owned_by` already covers
        a person owning an organization (including the hierarchy traversal
        a second role would lose); the AML/KYC distinction from ordinary
        equity ownership lives in `attributes`, the same convention
        `principal_of`'s `gp_role_key` uses."""
        edge = associations.associate(
            principal, role="owned_by", from_type="organization", from_id=str(gp["id"]),
            to_type="person", to_id=str(jane["id"]),
            attributes={"ownership_type": "beneficial"},
        )
        assert edge["attributes"]["ownership_type"] == "beneficial"

        blocks = associations.related_blocks(principal, "person", str(jane["id"]))
        assert blocks["owns"][0]["attributes"]["ownership_type"] == "beneficial"


class TestAccountLevelRoles:
    def test_a_person_can_hold_different_roles_at_gp_and_account_levels(
        self, principal, jane, gp, trust_account
    ):
        """The case Phase C exists for: Jane is a Principal at the GP AND
        the Trustee of one specific account -- two roles, two levels, one
        unchanged person record."""
        associations.associate(
            principal, role="principal_of", from_type="person", from_id=str(jane["id"]),
            to_type="organization", to_id=str(gp["id"]),
            attributes={"gp_role_key": "managing_partner"},
        )
        associations.associate(
            principal, role="trustee_of", from_type="person", from_id=str(jane["id"]),
            to_type="investment_account", to_id=str(trust_account["id"]),
        )

        roles = associations.roles_of(principal, "person", str(jane["id"]))
        assert roles == ["principal_of", "trustee_of"]

    def test_account_holder_and_beneficiary_are_distinct_roles(
        self, principal, jane, trust_account
    ):
        associations.associate(
            principal, role="account_holder_of", from_type="person",
            from_id=str(jane["id"]), to_type="investment_account",
            to_id=str(trust_account["id"]),
        )
        beneficiary = repository.create(principal, "person", {"full_name": "Minor Doe"})
        associations.associate(
            principal, role="beneficiary_of", from_type="person",
            from_id=str(beneficiary["id"]), to_type="investment_account",
            to_id=str(trust_account["id"]),
        )

        blocks = associations.related_blocks(
            principal, "investment_account", str(trust_account["id"])
        )
        assert "account holders" in blocks
        assert "beneficiaries" in blocks

    def test_an_as_of_query_shows_a_role_that_has_since_ended(
        self, principal, jane, trust_account
    ):
        edge = associations.associate(
            principal, role="trustee_of", from_type="person", from_id=str(jane["id"]),
            to_type="investment_account", to_id=str(trust_account["id"]),
            valid_from="2020-01-01",
        )
        associations.end_association(principal, str(edge["id"]), on="2024-06-30")

        assert associations.edges_for(
            principal, "person", str(jane["id"]), as_of="2022-01-01"
        )
        assert not associations.edges_for(
            principal, "person", str(jane["id"]), as_of="2025-01-01"
        )


class TestAuthorizedSignerSpansBothLevels:
    def test_the_same_person_can_sign_at_the_gp_and_on_an_account(
        self, principal, jane, gp, trust_account
    ):
        associations.associate(
            principal, role="authorized_signer_for", from_type="person",
            from_id=str(jane["id"]), to_type="organization", to_id=str(gp["id"]),
        )
        associations.associate(
            principal, role="authorized_signer_for", from_type="person",
            from_id=str(jane["id"]), to_type="investment_account",
            to_id=str(trust_account["id"]),
        )

        edges = associations.edges_for(
            principal, "person", str(jane["id"]), roles=["authorized_signer_for"]
        )
        assert {e["other_type"] for e in edges} == {"organization", "investment_account"}

    def test_the_role_rejects_endpoints_it_does_not_connect(self, principal, jane):
        fund = repository.create(principal, "fund", {"name": "Fund I"})
        with pytest.raises(associations.AssociationError):
            associations.associate(
                principal, role="authorized_signer_for", from_type="person",
                from_id=str(jane["id"]), to_type="fund", to_id=str(fund["id"]),
            )


class TestRegistry:
    def test_every_phase_c_role_is_registered_under_funds(self):
        for role_name in (
            "principal_of", "authorized_signer_for",
            "account_holder_of", "trustee_of", "beneficiary_of",
        ):
            assert registry.role(role_name).module == "funds"
