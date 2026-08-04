"""Phase 11: the generic `has_role` filter primitive (`query.py`'s
`_compile_role_clause`) and the funds module's seeded default saved views.

The filter itself is core (R6: no per-vertical code in `compile_filter()`),
exercised here against `modules/funds`' roles because that's where the
richest role vocabulary already lives -- matching `test_vertical_funds.py`'s
own convention of proving core mechanisms through the vertical rather than a
second, synthetic role just for the test.
"""
from __future__ import annotations

import pytest

from server.core import associations, permissions, query, registry, repository, users


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


class TestHasRoleFilter:
    def test_filters_organizations_by_an_outbound_role(self, principal, acme, brightline, fund_ii):
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        result = repository.list_records(
            principal, "organization",
            filters={"role": "portfolio_of", "direction": "from"},
        )
        names = {r["name"] for r in result["records"]}
        assert names == {"Brightline Capital"}

    def test_filters_organizations_by_an_inbound_role(self, principal, brightline, fund_ii):
        """`evaluating` is declared fund -> organization, so an organization
        only matches it as the 'to' side."""
        associations.associate(
            principal, role="evaluating", from_type="fund", from_id=str(fund_ii["id"]),
            to_type="organization", to_id=str(brightline["id"]),
        )
        result = repository.list_records(
            principal, "organization",
            filters={"role": "evaluating", "direction": "to"},
        )
        assert {r["name"] for r in result["records"]} == {"Brightline Capital"}

    def test_combines_with_and_or_not(self, principal, acme, brightline, fund_ii):
        """The seeded 'Public markets we track' view's own shape: public AND
        (portfolio_of OR evaluating)."""
        repository.update(principal, "organization", str(brightline["id"]), {"is_public": True})
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        # A public company with no investing role at all must not match.
        repository.update(principal, "organization", str(acme["id"]), {"is_public": True})

        result = repository.list_records(
            principal, "organization",
            filters={"and": [
                {"field": "is_public", "op": "eq", "value": True},
                {"or": [
                    {"role": "portfolio_of", "direction": "from"},
                    {"role": "evaluating", "direction": "to"},
                ]},
            ]},
        )
        assert {r["name"] for r in result["records"]} == {"Brightline Capital"}

    def test_not_role_excludes_matching_records(self, principal, acme, brightline, fund_ii):
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        result = repository.list_records(
            principal, "organization",
            filters={"not": {"role": "portfolio_of", "direction": "from"}},
        )
        assert {r["name"] for r in result["records"]} == {"Acme Logistics"}

    def test_a_symmetric_role_matches_either_stored_side(self, principal, acme, brightline):
        """`co_investor_in` is canonicalized on write (endpoints sorted), so
        whichever of the two ends up as `from`/`to` in storage, filtering
        from either organization's perspective must still find the edge."""
        associations.associate(
            principal, role="co_investor_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="organization", to_id=str(acme["id"]),
        )
        result = repository.list_records(
            principal, "organization",
            filters={"role": "co_investor_in", "direction": "to"},
        )
        assert {r["name"] for r in result["records"]} == {"Acme Logistics", "Brightline Capital"}

    def test_role_filter_requires_read_on_the_other_side_entity_type(
        self, org_id, member, acme, fund_ii
    ):
        """A `role` filter is an EXISTS with no join into the other side's
        row, so nothing else would catch a principal with no grant on that
        entity type at all -- without this check, `{"role": "portfolio_of",
        "direction": "from"}` would silently reveal which organizations have
        SOME fund relationship to someone who cannot read `fund` at all,
        purely from which rows come back (the same disclosure-by-inclusion
        shape `permissions.require_field_readable` already guards against
        for an ordinary field filter)."""
        role = users.create_role(org_id, "Org Reader")
        users.set_role_scope(org_id, str(role["id"]), "organization", read_level="all")
        # Deliberately no scope row at all for "fund" -- default deny.
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        member_principal = permissions.load_principal(member)

        assert member_principal.for_object("fund").read_level == "none"
        with pytest.raises(permissions.PermissionDenied):
            repository.list_records(
                member_principal, "organization",
                filters={"role": "portfolio_of", "direction": "from"},
            )

    def test_role_filter_succeeds_once_the_other_side_is_granted(
        self, principal, org_id, member, brightline, fund_ii
    ):
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        role = users.create_role(org_id, "Investing Reader")
        users.set_role_scope(org_id, str(role["id"]), "organization", read_level="all")
        users.set_role_scope(org_id, str(role["id"]), "fund", read_level="all")
        users.assign_role(org_id, str(member["id"]), str(role["id"]))
        member_principal = permissions.load_principal(member)

        result = repository.list_records(
            member_principal, "organization",
            filters={"role": "portfolio_of", "direction": "from"},
        )
        assert {r["name"] for r in result["records"]} == {"Brightline Capital"}

    def test_unknown_role_raises(self, principal):
        with pytest.raises(registry.UnknownField):
            repository.list_records(
                principal, "organization",
                filters={"role": "not_a_real_role", "direction": "from"},
            )

    def test_wrong_entity_for_the_role_and_direction_raises(self, principal):
        """`evaluating` only accepts `organization` as its 'to' side --
        asking for it as 'from' on organization must raise, not silently
        match nothing (the same disclosure-safe discipline as an unknown
        field)."""
        with pytest.raises(query.FilterError):
            repository.list_records(
                principal, "organization",
                filters={"role": "evaluating", "direction": "from"},
            )

    def test_bad_direction_raises(self, principal):
        with pytest.raises(query.FilterError):
            repository.list_records(
                principal, "organization",
                filters={"role": "portfolio_of", "direction": "sideways"},
            )

    def test_as_of_excludes_an_association_that_has_already_ended(
        self, principal, brightline, fund_ii
    ):
        edge = associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        associations.end_association(principal, str(edge["id"]), on="2020-01-01")

        current = repository.list_records(
            principal, "organization",
            filters={"role": "portfolio_of", "direction": "from"},
        )
        assert current["total"] == 0

        historical = repository.list_records(
            principal, "organization",
            filters={"role": "portfolio_of", "direction": "from", "as_of": "2019-06-01"},
        )
        assert historical["total"] == 1

        with_history = repository.list_records(
            principal, "organization",
            filters={"role": "portfolio_of", "direction": "from", "include_history": True},
        )
        assert with_history["total"] == 1


class TestSeededSavedViews:
    def test_creating_an_org_seeds_the_default_views(self, principal):
        result = repository.list_records(principal, "saved_view")
        assert result["total"] == 9
        by_name = {r["name"]: r for r in result["records"]}
        assert set(by_name) == {
            "Portfolio companies", "Prospects we're evaluating",
            "Public markets we track", "LP organizations",
            "Counterparties & vendors", "GP team", "LP individuals",
            "Board members & advisors", "Ordinary contacts",
        }
        assert by_name["Portfolio companies"]["entity"] == "organization"
        assert by_name["GP team"]["entity"] == "person"

    def test_seeded_views_are_ordinary_editable_rows(self, principal):
        """R4: no bespoke seeding path -- these are the same generic
        create()/update() every other saved_view uses."""
        result = repository.list_records(
            principal, "saved_view",
            filters={"field": "name", "op": "eq", "value": "Portfolio companies"},
        )
        view = result["records"][0]
        updated = repository.update(
            principal, "saved_view", str(view["id"]), {"name": "Renamed view"}
        )
        assert updated["name"] == "Renamed view"

    def test_the_portfolio_companies_view_executes_correctly(
        self, principal, acme, brightline, fund_ii
    ):
        associations.associate(
            principal, role="portfolio_of", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        view = repository.list_records(
            principal, "saved_view",
            filters={"field": "name", "op": "eq", "value": "Portfolio companies"},
        )["records"][0]

        result = repository.list_records(principal, "organization", filters=view["filters"])
        assert {r["name"] for r in result["records"]} == {"Brightline Capital"}

    def test_the_counterparties_view_excludes_every_investing_role(
        self, principal, acme, brightline, fund_ii
    ):
        associations.associate(
            principal, role="lp_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
        )
        view = repository.list_records(
            principal, "saved_view",
            filters={"field": "name", "op": "eq", "value": "Counterparties & vendors"},
        )["records"][0]

        result = repository.list_records(principal, "organization", filters=view["filters"])
        assert {r["name"] for r in result["records"]} == {"Acme Logistics"}
