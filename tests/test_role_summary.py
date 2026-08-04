"""`role_summary` -- an organization's/person's emergent "type," computed live
from its association-role graph via `context_builder_ids` (registry.py) and
`associations.role_summary_for()`, never persisted (R6: "a role is not an
entity type").
"""
from __future__ import annotations

from server.core import associations, repository
from server.db import pool


def test_role_summary_reflects_every_role_a_record_currently_plays(principal):
    brightline = repository.create(principal, "organization", {"name": "Brightline Capital"})
    fund_ii = repository.create(
        principal, "fund",
        {"name": "Northgate Fund II", "vintage_year": 2026, "strategy": "growth",
         "target_size": 250_000_000, "status": "raising"},
    )
    other_fund = repository.create(principal, "fund", {"name": "Northgate Fund I"})

    associations.associate(
        principal, role="lp_in", from_type="organization",
        from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
    )
    associations.associate(
        principal, role="portfolio_of", from_type="organization",
        from_id=str(brightline["id"]), to_type="fund", to_id=str(other_fund["id"]),
    )

    fetched = repository.get_record(principal, "organization", str(brightline["id"]))
    labels = {r["label"] for r in fetched["role_summary"]}
    assert labels == {"LP in", "Portfolio of"}


def test_role_summary_collapses_duplicate_roles_into_one_entry(principal):
    """An LP in three funds is one "LP in" pill, not three -- this is a type
    summary, not the detailed edge list `related_blocks()` already gives."""
    brightline = repository.create(principal, "organization", {"name": "Brightline Capital"})
    for i in range(3):
        fund = repository.create(principal, "fund", {"name": f"Fund {i}"})
        associations.associate(
            principal, role="lp_in", from_type="organization",
            from_id=str(brightline["id"]), to_type="fund", to_id=str(fund["id"]),
        )

    fetched = repository.get_record(principal, "organization", str(brightline["id"]))
    assert [r["label"] for r in fetched["role_summary"]] == ["LP in"]


def test_role_summary_is_absent_for_a_record_with_no_associations(principal):
    acme = repository.create(principal, "organization", {"name": "Acme Logistics"})
    fetched = repository.get_record(principal, "organization", str(acme["id"]))
    assert fetched["role_summary"] == []


def test_a_list_page_batches_role_summary_into_one_query_not_one_per_row(principal):
    """`context_builder_ids` exists precisely so a page of N rows costs one
    extra query, not N -- assert that directly rather than trusting it."""
    brightline = repository.create(principal, "organization", {"name": "Brightline Capital"})
    fund_ii = repository.create(
        principal, "fund",
        {"name": "Northgate Fund II", "vintage_year": 2026, "strategy": "growth",
         "target_size": 250_000_000, "status": "raising"},
    )
    associations.associate(
        principal, role="lp_in", from_type="organization",
        from_id=str(brightline["id"]), to_type="fund", to_id=str(fund_ii["id"]),
    )
    for i in range(5):
        repository.create(principal, "organization", {"name": f"Ordinary Co {i}"})

    calls = []
    original = associations.role_summary_for

    def _counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    associations.role_summary_for = _counting
    try:
        result = repository.list_records(principal, "organization")
    finally:
        associations.role_summary_for = original

    assert result["total"] == 6
    assert len(calls) == 1

    by_name = {r["name"]: r for r in result["records"]}
    assert [r["label"] for r in by_name["Brightline Capital"]["role_summary"]] == ["LP in"]
    assert by_name["Ordinary Co 0"]["role_summary"] == []
