"""Public/private target companies (Phase 10): `organization.is_public` (a
domain-neutral core field), `modules/funds`' `security` entity (ticker/
exchange -- lifecycle-shaped, so it lives off core), and the `evaluating`
role that lets a fund track a prospective target before it becomes an actual
`portfolio_of` investment.
"""
from __future__ import annotations

import pytest

from server.core import associations, registry, repository


@pytest.fixture
def fund_ii(principal):
    return repository.create(
        principal, "fund",
        {"name": "Northgate Fund II", "vintage_year": 2026, "strategy": "growth",
         "target_size": 250_000_000, "status": "raising"},
    )


class TestIsPublicIsDomainNeutral:
    def test_is_public_defaults_false_and_is_writable_through_the_generic_path(
        self, principal
    ):
        org = repository.create(principal, "organization", {"name": "Acme Logistics"})
        assert org["is_public"] is False

        updated = repository.update(
            principal, "organization", str(org["id"]), {"is_public": True}
        )
        assert updated["is_public"] is True

    def test_is_public_is_registered_on_core_not_a_module(self):
        assert registry.entity("organization").module == "core"
        assert registry.entity("organization").field("is_public").kind == "boolean"


class TestSecurityIsAModuleEntityNotACoreColumn:
    def test_security_is_registered_under_funds(self):
        assert registry.entity("security").module == "funds"
        assert registry.entity("security").nav == "none"

    def test_ticker_is_normalized_on_write(self, principal):
        org = repository.create(
            principal, "organization", {"name": "Public Co", "is_public": True}
        )
        security = repository.create(
            principal, "security",
            {"organization_id": str(org["id"]), "ticker": " pubc ", "exchange": "NASDAQ"},
        )
        assert security["ticker"] == "PUBC"

    def test_security_is_reachable_as_the_organizations_child(self, principal):
        """R4: `organization_id`'s `references="organization"` is what makes
        this show up in `repository.children_of()` with no new code -- the
        same treatment `contact_channel` gets from `person`."""
        org = repository.create(
            principal, "organization", {"name": "Public Co", "is_public": True}
        )
        repository.create(
            principal, "security",
            {"organization_id": str(org["id"]), "ticker": "PUBC", "exchange": "NASDAQ"},
        )
        blocks = repository.children_of(principal, "organization", str(org["id"]))
        by_entity = {b["entity"]: b for b in blocks}
        assert by_entity["security"]["total"] == 1
        assert by_entity["security"]["records"][0]["ticker"] == "PUBC"


class TestEvaluatingIsARoleNotAStatus:
    def test_a_fund_evaluates_an_organization(self, principal, fund_ii):
        target = repository.create(principal, "organization", {"name": "Target Co"})
        associations.associate(
            principal, role="evaluating", from_type="fund", from_id=str(fund_ii["id"]),
            to_type="organization", to_id=str(target["id"]),
        )

        from_fund = associations.related_blocks(principal, "fund", str(fund_ii["id"]))
        assert from_fund["Evaluating"][0]["record"]["name"] == "Target Co"

        from_target = associations.related_blocks(principal, "organization", str(target["id"]))
        assert from_target["being evaluated by"][0]["record"]["name"] == "Northgate Fund II"

    def test_evaluating_and_portfolio_of_can_coexist_and_transition_is_two_calls(
        self, principal, fund_ii
    ):
        """No status field, no validator coupling the two -- ending the
        evaluation and creating the investment are two independent writes,
        the same shape a commitment and its `lp_in` association already
        have today."""
        target = repository.create(principal, "organization", {"name": "Target Co"})
        edge = associations.associate(
            principal, role="evaluating", from_type="fund", from_id=str(fund_ii["id"]),
            to_type="organization", to_id=str(target["id"]),
        )

        # An explicit past end date: `valid_to` is inclusive (`_as_of_clause`'s
        # `valid_to >= when`), so ending it "as of today" would still count as
        # active for an as-of-today query -- ending it in the past is what
        # actually drops it from the current set, the case this test means to
        # cover.
        associations.end_association(principal, str(edge["id"]), on="2020-01-01")
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(target["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )

        roles = associations.roles_of(principal, "organization", str(target["id"]))
        assert roles == ["portfolio_of"]

        # The evaluating edge itself is preserved (ended, not deleted) --
        # history answers "was this a prospect before it was invested".
        history = associations.related_blocks(
            principal, "fund", str(fund_ii["id"]), include_history=True,
        )
        assert history["Evaluating"][0]["record"]["name"] == "Target Co"
