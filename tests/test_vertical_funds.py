"""The asset-management vertical, and the architecture claims it tests.

This file is the proof for M1. If `modules/funds` needed a change under
`server/core/` to work, the seam is in the wrong place — so R6 is asserted here
as a test rather than left as a docstring.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from server.core import associations, permissions, registry, repository


@pytest.fixture
def acme(principal):
    return repository.create(principal, "organization", {"name": "Acme Logistics"})


@pytest.fixture
def brightline(principal):
    return repository.create(principal, "organization", {"name": "Brightline Capital"})


@pytest.fixture
def fund_ii(principal):
    return repository.create(
        principal, "fund",
        {"name": "Northgate Fund II", "vintage_year": 2026, "strategy": "growth",
         "target_size": 250_000_000, "status": "raising"},
    )


class TestModuleSeam:
    def test_the_module_registers_without_core_knowing_it_exists(self):
        assert "fund" in registry.entities()
        assert "commitment" in registry.entities()
        assert registry.entity("fund").module == "funds"
        assert registry.role("lp_in").module == "funds"

    def test_core_never_mentions_a_fund(self):
        """R6. If this fails, the vertical is leaking into the generic core and
        the next vertical becomes a fork rather than a module.

        Tokenized rather than grepped: prose is allowed to explain the rule --
        several core docstrings discuss why funds live in a module -- but no
        identifier, attribute or string literal in core may name one.
        """
        import io
        import tokenize

        vocabulary = re.compile(
            r"\b(fund|funds|commitment|commitments|lp_in|portfolio_of|vintage|"
            r"portco|capital_call|carried_interest)\b",
            re.IGNORECASE,
        )
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for path in sorted((root / "server").rglob("*.py")):
            source = path.read_text()
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                    continue
                if vocabulary.search(token.string):
                    offenders.append(
                        f"{path.relative_to(root)}:{token.start[0]}: {token.string}"
                    )
        assert offenders == [], "vertical vocabulary in core:\n" + "\n".join(offenders)

    def test_module_entities_get_crud_with_no_new_routes(self, principal, fund_ii):
        """R4: a new entity is a registry entry. The module wrote no repository
        code, no validation and no routes, and all of it works."""
        fetched = repository.get_record(principal, "fund", str(fund_ii["id"]))
        assert fetched["name"] == "Northgate Fund II"

        updated = repository.update(
            principal, "fund", str(fund_ii["id"]), {"status": "investing"}
        )
        assert updated["status"] == "investing"

        listed = repository.list_records(
            principal, "fund", filters={"field": "vintage_year", "op": "gte", "value": 2020}
        )
        assert listed["total"] == 1

    def test_module_fields_are_validated_by_the_generic_path(self, principal):
        with pytest.raises(repository.ValidationError):
            repository.create(principal, "fund", {"name": "F", "status": "invented"})
        with pytest.raises(repository.ValidationError):
            repository.create(principal, "fund", {"name": "F", "first_close_at": "2026-13-45"})

    def test_module_tables_are_tenant_isolated_like_core(self):
        from server.db import schema

        assert schema.verify_rls() == []
        assert "core.funds" in schema.all_tables()
        assert "core.funds" not in schema.UNSCOPED_TABLES


class TestWhoInvestedInWhichFund:
    def test_a_commitment_records_who_committed_what(self, principal, fund_ii, brightline):
        commitment = repository.create(
            principal, "commitment",
            {"fund_id": str(fund_ii["id"]), "investor_org_id": str(brightline["id"]),
             "amount": 25_000_000, "committed_at": "2026-03-01", "status": "signed"},
        )
        assert float(commitment["amount"]) == 25_000_000

        by_fund = repository.list_records(
            principal, "commitment",
            filters={"field": "fund_id", "op": "eq", "value": str(fund_ii["id"])},
        )
        assert by_fund["total"] == 1

    def test_an_lp_relationship_reads_from_both_ends(self, principal, fund_ii, brightline):
        """One stored row. The fund shows its investors and the investor shows
        its funds -- without a mirror row that could drift."""
        associations.associate(
            principal, role="lp_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
            valid_from="2026-03-01",
        )

        from_investor = associations.related_blocks(
            principal, "organization", str(brightline["id"])
        )
        assert "lp_in" in from_investor
        assert from_investor["lp_in"][0]["record"]["name"] == "Northgate Fund II"

        from_fund = associations.related_blocks(principal, "fund", str(fund_ii["id"]))
        assert "investors" in from_fund, "the inverse label should name the block"
        assert from_fund["investors"][0]["record"]["name"] == "Brightline Capital"

    def test_people_at_a_company_carry_their_title_on_the_relationship(
        self, principal, brightline
    ):
        person = repository.create(principal, "person", {"full_name": "Marcus Oyelaran"})
        associations.associate(
            principal, role="works_at", from_type="person", from_id=str(person["id"]),
            to_type="organization", to_id=str(brightline["id"]),
            attributes={"title": "Managing Director"}, valid_from="2021-06-01",
        )
        blocks = associations.related_blocks(
            principal, "organization", str(brightline["id"])
        )
        assert blocks["employs"][0]["attributes"]["title"] == "Managing Director"


class TestRolesNotEntityTypes:
    def test_one_organization_holds_several_roles_at_once(
        self, principal, acme, brightline, fund_ii
    ):
        """The case the whole data model exists for. With an `investors` table
        and a `portfolio_companies` table this would be three duplicate records
        with three separate interaction histories."""
        other_fund = repository.create(principal, "fund", {"name": "Northgate Fund I"})

        associations.associate(
            principal, role="lp_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]))
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund",
            to_id=str(other_fund["id"]))
        associations.associate(
            principal, role="co_investor_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="organization",
            to_id=str(acme["id"]))

        roles = associations.roles_of(principal, "organization", str(brightline["id"]))
        assert roles == ["co_investor_in", "lp_in", "portfolio_of"]

    def test_a_symmetric_role_is_stored_once_whichever_way_it_is_written(
        self, principal, acme, brightline
    ):
        """Without canonicalization, A/B and B/A are two rows the uniqueness
        index cannot see as duplicates, and every co-investment is double
        counted."""
        first = associations.associate(
            principal, role="co_investor_in", from_type="organization",
            from_id=str(acme["id"]), to_type="organization", to_id=str(brightline["id"]))
        second = associations.associate(
            principal, role="co_investor_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="organization", to_id=str(acme["id"]))
        assert str(first["id"]) == str(second["id"])

        edges = associations.edges_for(principal, "organization", str(acme["id"]))
        assert len(edges) == 1

    def test_a_role_rejects_endpoints_it_does_not_connect(self, principal, acme, fund_ii):
        with pytest.raises(associations.AssociationError):
            associations.associate(
                principal, role="works_at", from_type="organization",
                from_id=str(acme["id"]), to_type="fund", to_id=str(fund_ii["id"]))

    def test_an_unknown_role_raises(self, principal, acme, brightline):
        with pytest.raises(registry.UnknownField):
            associations.associate(
                principal, role="invented_role", from_type="organization",
                from_id=str(acme["id"]), to_type="organization",
                to_id=str(brightline["id"]))


class TestDating:
    def test_an_ended_relationship_drops_out_of_the_present(
        self, principal, brightline
    ):
        person = repository.create(principal, "person", {"full_name": "Former CFO"})
        edge = associations.associate(
            principal, role="works_at", from_type="person", from_id=str(person["id"]),
            to_type="organization", to_id=str(brightline["id"]),
            valid_from="2020-01-01",
        )
        associations.end_association(principal, str(edge["id"]), on="2024-06-30")

        assert associations.edges_for(principal, "person", str(person["id"])) == []
        historic = associations.edges_for(
            principal, "person", str(person["id"]), include_history=True
        )
        assert len(historic) == 1

    def test_as_of_answers_what_was_true_then(self, principal, brightline):
        person = repository.create(principal, "person", {"full_name": "Former CFO"})
        edge = associations.associate(
            principal, role="works_at", from_type="person", from_id=str(person["id"]),
            to_type="organization", to_id=str(brightline["id"]), valid_from="2020-01-01")
        associations.end_association(principal, str(edge["id"]), on="2024-06-30")

        assert associations.edges_for(
            principal, "person", str(person["id"]), as_of="2022-01-01"
        )
        assert not associations.edges_for(
            principal, "person", str(person["id"]), as_of="2025-01-01"
        )

    def test_the_same_pair_can_hold_successive_stints(self, principal, brightline):
        person = repository.create(principal, "person", {"full_name": "Boomerang"})
        for start, end in [("2018-01-01", "2020-01-01"), ("2023-01-01", None)]:
            edge = associations.associate(
                principal, role="works_at", from_type="person",
                from_id=str(person["id"]), to_type="organization",
                to_id=str(brightline["id"]), valid_from=start)
            if end:
                associations.end_association(principal, str(edge["id"]), on=end)

        assert len(associations.edges_for(
            principal, "person", str(person["id"]), include_history=True)) == 2


class TestAssociationPermissions:
    def test_hidden_related_records_are_absent_not_listed_as_hidden(
        self, org_id, principal, member, brightline
    ):
        """Hydration goes through the repository, so a related record the
        principal cannot read simply does not appear. A SQL join would render
        its name; a "3 hidden" note would leak that it exists."""
        from server.core import users

        person = repository.create(principal, "person", {"full_name": "Secret Contact"})
        associations.associate(
            principal, role="works_at", from_type="person", from_id=str(person["id"]),
            to_type="organization", to_id=str(brightline["id"]))

        role = users.create_role(org_id, "Limited")
        users.set_role_scope(org_id, str(role["id"]), "organization", read_level="all")
        users.set_role_scope(org_id, str(role["id"]), "person", read_level="own")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        actor = permissions.load_principal(member)

        blocks = associations.related_blocks(
            actor, "organization", str(brightline["id"])
        )
        assert blocks == {}

    def test_linking_needs_edit_on_the_near_side(self, org_id, member, principal, brightline):
        from server.core import users

        person = repository.create(principal, "person", {"full_name": "Someone"})
        role = users.create_role(org_id, "ReadOnly")
        for entity in ("person", "organization"):
            users.set_role_scope(org_id, str(role["id"]), entity, read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        actor = permissions.load_principal(member)

        with pytest.raises(permissions.PermissionDenied):
            associations.associate(
                actor, role="works_at", from_type="person", from_id=str(person["id"]),
                to_type="organization", to_id=str(brightline["id"]))
