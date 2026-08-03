"""Phase E: modules/funds/backfill.py points every pre-Phase-B commitment
at a synthesized investment_account (and that account at a synthesized
placeholder Investment GP organization), safely and idempotently.
"""
from __future__ import annotations

import pytest

from modules.funds import backfill
from server.core import associations, repository


@pytest.fixture
def fund(principal):
    return repository.create(principal, "fund", {"name": "Northgate Fund II"})


@pytest.fixture
def other_fund(principal):
    return repository.create(principal, "fund", {"name": "Northgate Fund I"})


@pytest.fixture
def investor_org(principal):
    return repository.create(principal, "organization", {"name": "Brightline Capital"})


@pytest.fixture
def investor_person(principal):
    return repository.create(principal, "person", {"full_name": "Jane Doe"})


class TestOrgHeldCommitment:
    def test_backfill_synthesizes_an_account_and_gp_rollup(
        self, org_id, principal, fund, investor_org
    ):
        commitment = repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_org_id": str(investor_org["id"]),
             "amount": 1_000_000, "status": "signed"},
        )

        counts = backfill.backfill_commitments_to_accounts(org_id)
        assert counts == {
            "commitments_updated": 1, "accounts_created": 1,
            "gp_organizations_created": 1, "commitments_skipped_no_investor": 0,
        }

        updated = repository.get_record(principal, "commitment", str(commitment["id"]))
        assert updated["investment_account_id"] is not None

        account = repository.get_record(
            principal, "investment_account", str(updated["investment_account_id"])
        )
        assert account["account_type"] == "other"
        assert str(account["entity_org_id"]) == str(investor_org["id"])
        assert account["name"] == investor_org["name"]

        ancestors = associations.edges_for(
            principal, "investment_account", str(account["id"]), roles=["rolls_up_to"]
        )
        assert len(ancestors) == 1
        gp = repository.get_record(principal, "organization", str(ancestors[0]["other_id"]))
        assert gp["is_derived"] is True
        assert gp["name"] == f"{investor_org['name']} (GP)"


class TestPersonHeldCommitment:
    def test_backfill_synthesizes_an_individual_account(
        self, org_id, principal, fund, investor_person
    ):
        commitment = repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_person_id": str(investor_person["id"]),
             "amount": 250_000, "status": "signed"},
        )

        counts = backfill.backfill_commitments_to_accounts(org_id)
        assert counts["commitments_updated"] == 1
        assert counts["accounts_created"] == 1

        updated = repository.get_record(principal, "commitment", str(commitment["id"]))
        account = repository.get_record(
            principal, "investment_account", str(updated["investment_account_id"])
        )
        assert account["account_type"] == "individual"
        assert str(account["person_id"]) == str(investor_person["id"])


class TestDedup:
    def test_the_same_investor_across_two_funds_gets_one_account(
        self, org_id, principal, fund, other_fund, investor_org
    ):
        c1 = repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_org_id": str(investor_org["id"]),
             "amount": 1_000_000},
        )
        c2 = repository.create(
            principal, "commitment",
            {"fund_id": str(other_fund["id"]), "investor_org_id": str(investor_org["id"]),
             "amount": 2_000_000},
        )

        counts = backfill.backfill_commitments_to_accounts(org_id)
        assert counts["commitments_updated"] == 2
        assert counts["accounts_created"] == 1
        assert counts["gp_organizations_created"] == 1

        a1 = repository.get_record(principal, "commitment", str(c1["id"]))
        a2 = repository.get_record(principal, "commitment", str(c2["id"]))
        assert str(a1["investment_account_id"]) == str(a2["investment_account_id"])

    def test_an_investment_account_a_human_already_made_is_reused(
        self, org_id, principal, fund, investor_org
    ):
        existing_account = repository.create(
            principal, "investment_account",
            {"name": "Brightline (existing)", "entity_org_id": str(investor_org["id"]),
             "account_type": "lp"},
        )
        commitment = repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_org_id": str(investor_org["id"]),
             "amount": 500_000},
        )

        counts = backfill.backfill_commitments_to_accounts(org_id)
        assert counts["accounts_created"] == 0

        updated = repository.get_record(principal, "commitment", str(commitment["id"]))
        assert str(updated["investment_account_id"]) == str(existing_account["id"])


class TestIdempotency:
    def test_running_it_twice_makes_no_further_changes(
        self, org_id, principal, fund, investor_org
    ):
        repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_org_id": str(investor_org["id"]),
             "amount": 1_000_000},
        )

        first = backfill.backfill_commitments_to_accounts(org_id)
        assert first["commitments_updated"] == 1
        assert first["accounts_created"] == 1
        assert first["gp_organizations_created"] == 1

        second = backfill.backfill_commitments_to_accounts(org_id)
        assert second == {
            "commitments_updated": 0, "accounts_created": 0,
            "gp_organizations_created": 0, "commitments_skipped_no_investor": 0,
        }


class TestNoInvestor:
    def test_a_commitment_with_no_investor_at_all_is_skipped_not_looped_forever(
        self, org_id, principal, fund
    ):
        repository.create(
            principal, "commitment", {"fund_id": str(fund["id"]), "amount": 100_000}
        )
        counts = backfill.backfill_commitments_to_accounts(org_id)
        assert counts == {
            "commitments_updated": 0, "accounts_created": 0,
            "gp_organizations_created": 0, "commitments_skipped_no_investor": 1,
        }


class TestScoping:
    def test_backfill_only_touches_its_own_org(
        self, org_id, principal, fund, investor_org
    ):
        from server.core import permissions, users

        other_org = users.create_org("Other Org", "other-backfill-org")
        other_admin = users.create_user(
            other_org["id"], email="admin4@example.com", name="Admin Four",
            password="correct-horse-battery", is_admin=True,
        )
        other_principal = permissions.load_principal(other_admin)
        other_fund = repository.create(other_principal, "fund", {"name": "Unrelated Fund"})
        other_investor = repository.create(
            other_principal, "organization", {"name": "Unrelated Investor"}
        )
        repository.create(
            other_principal, "commitment",
            {"fund_id": str(other_fund["id"]), "investor_org_id": str(other_investor["id"]),
             "amount": 1},
        )
        repository.create(
            principal, "commitment",
            {"fund_id": str(fund["id"]), "investor_org_id": str(investor_org["id"]),
             "amount": 1_000_000},
        )

        counts = backfill.backfill_commitments_to_accounts(org_id)
        assert counts["commitments_updated"] == 1

        other_result = repository.list_records(
            other_principal, "commitment",
            filters={"field": "investment_account_id", "op": "is_null"},
        )
        assert other_result["total"] == 1, "the other org's commitment must be untouched"


class TestLpInAcceptsInvestmentAccount:
    def test_an_investment_account_can_be_tagged_lp_in_a_fund(
        self, principal, fund, investor_org
    ):
        account = repository.create(
            principal, "investment_account",
            {"name": "Direct Account", "entity_org_id": str(investor_org["id"]),
             "account_type": "lp"},
        )
        edge = associations.associate(
            principal, role="lp_in", from_type="investment_account",
            from_id=str(account["id"]), to_type="fund", to_id=str(fund["id"]),
        )
        assert edge["from_type"] == "investment_account"
