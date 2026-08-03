"""Phase E: point every pre-Phase-B commitment at an `investment_account`.

## Why this is a script, not part of install()/migrate()

`server/db/schema.py`'s `migrate()` is schema-only (DDL: tables, indexes,
RLS), run unconditionally on every app start. This codebase's established
convention is to design tables so the ordinary write path is idempotent and
a data backfill is never needed (see `ai.embeddings`' content-hash unique
key, or `investor_portal`'s mandate-derivation upsert) -- there is no other
row-data backfill anywhere in this codebase. A one-time transform over an
existing `commitments` table predating `investment_account` is a genuine
exception to that, so it stays an explicit, operator-run action: CLAUDE.md
safety rule 9 ("destructive/consequential paths default off") applied to
"run this migration," not just to a runtime toggle.

## Idempotency

Safe to run repeatedly, including after a partial/interrupted run:

- Only commitments with `investment_account_id IS NULL` are ever touched;
  a commitment that already has one falls out of the query on the very
  next page, so re-running never re-processes or double-applies anything.
- An investor with more than one existing commitment (across funds) gets
  exactly ONE synthesized `investment_account` -- found by an
  `entity_org_id`/`person_id` lookup before ever creating one, so re-runs
  and multi-commitment investors alike converge on the same account rather
  than creating duplicates.
- The synthesized "Investment GP" rollup is checked the same way (an
  existing `rolls_up_to` edge is left alone) every time this is called for
  a given account, so a retry after the account was created but before its
  rollup edge landed still completes the rollup rather than skipping it or
  doubling it.

## What "synthesized" means, concretely

Pre-Phase-B, a commitment names its investor directly (`investor_org_id`
or `investor_person_id`) with no vehicle layer and no GP-level rollup at
all -- there is no real data here to migrate FROM beyond "who invested."
So the backfill doesn't attempt to guess a real Investment GP structure; it
gives each distinct investor exactly one investment_account
(`account_type='individual'` for a person, `'other'` for an organization --
"which kind of entity" genuinely isn't knowable from a bare
`investor_org_id`) and one placeholder GP organization for that account to
roll up to, both marked `is_derived=True`/`source='derived'` so an operator
can find every one of them later and merge/rename/restructure as the real
GP structure becomes known -- the same "derived until a human confirms it"
convention `core.organizations.is_derived` already carries elsewhere.

## What this deliberately does NOT do

Retire `commitments.investor_org_id`/`investor_person_id`. Those columns
stay until every row in a real deployment has been backfilled and verified
-- dropping them is a separate, later, destructive migration step, not
this one (see the schema comment on `core.commitments.investment_account_id`
in `modules/funds/manifest.py`).
"""
from __future__ import annotations

from typing import Any

from server.core import associations, permissions, query, repository

_GP_SUFFIX = " (GP)"


def backfill_commitments_to_accounts(org_id: str) -> dict[str, int]:
    """Run the backfill for one org. Returns counts for an operator to
    read back, not just silent success -- how many commitments were
    updated, how many accounts/GP organizations were newly synthesized,
    and how many commitments had no investor at all to migrate onto
    anything (pre-existing, orphaned rows this can't fix).
    """
    principal = permissions.system_principal(
        org_id, "backfill commitments onto investment accounts"
    )
    counts = {
        "commitments_updated": 0,
        "accounts_created": 0,
        "gp_organizations_created": 0,
        "commitments_skipped_no_investor": 0,
    }

    while True:
        # No offset: each successfully-processed commitment falls out of
        # this filter on the next iteration, so re-querying from the start
        # of the (shrinking) result set is correct -- offset-based paging
        # here would silently skip rows as the filtered set shrinks under it.
        page = repository.list_records(
            principal, "commitment",
            filters={"field": "investment_account_id", "op": "is_null"},
            limit=query.MAX_LIMIT,
        )
        if not page["records"]:
            break

        progressed = False
        for commitment in page["records"]:
            investor_org_id = commitment.get("investor_org_id")
            investor_person_id = commitment.get("investor_person_id")
            if not investor_org_id and not investor_person_id:
                counts["commitments_skipped_no_investor"] += 1
                continue

            account_id, created = _find_or_create_account(
                principal, investor_org_id, investor_person_id,
            )
            if created:
                counts["accounts_created"] += 1
            counts["gp_organizations_created"] += _ensure_rollup(principal, account_id)

            repository.update(
                principal, "commitment", str(commitment["id"]),
                {"investment_account_id": account_id},
            )
            counts["commitments_updated"] += 1
            progressed = True

        # Every record in this page was unresolvable (no investor at all) --
        # they'll never leave the is_null filter, so stop rather than loop
        # on the same unfixable rows forever.
        if not progressed:
            break

    return counts


def _find_or_create_account(
    principal: permissions.Principal,
    investor_org_id: Any,
    investor_person_id: Any,
) -> tuple[str, bool]:
    """One investment_account per distinct investor. Reuses whatever
    already exists for this org/person -- created by an earlier backfill
    pass, or by a human through the ordinary Phase B/C UI -- rather than
    ever creating a second one for the same investor."""
    if investor_org_id:
        existing = repository.list_records(
            principal, "investment_account",
            filters={"field": "entity_org_id", "op": "eq", "value": str(investor_org_id)},
            limit=1,
        )
        if existing["records"]:
            return str(existing["records"][0]["id"]), False
        org = repository.get_record(principal, "organization", str(investor_org_id))
        account = repository.create(
            principal, "investment_account",
            {"name": org["name"], "entity_org_id": str(investor_org_id),
             "account_type": "other"},
        )
        return str(account["id"]), True

    existing = repository.list_records(
        principal, "investment_account",
        filters={"field": "person_id", "op": "eq", "value": str(investor_person_id)},
        limit=1,
    )
    if existing["records"]:
        return str(existing["records"][0]["id"]), False
    person = repository.get_record(principal, "person", str(investor_person_id))
    account = repository.create(
        principal, "investment_account",
        {"name": person["full_name"], "person_id": str(investor_person_id),
         "account_type": "individual"},
    )
    return str(account["id"]), True


def _ensure_rollup(principal: permissions.Principal, account_id: str) -> int:
    """Give `account_id` a placeholder Investment GP to roll up to, unless
    it already has one. Returns 1 if a new GP organization was created, 0
    if an existing rollup edge was left alone -- makes this (and therefore
    the whole backfill) safe to call again for an account whose account
    creation succeeded on a prior run but whose rollup step didn't."""
    existing = associations.edges_for(
        principal, "investment_account", account_id, roles=["rolls_up_to"]
    )
    if existing:
        return 0

    account = repository.get_record(principal, "investment_account", account_id)
    gp = repository.create(
        principal, "organization",
        {"name": f"{account['name']}{_GP_SUFFIX}", "is_derived": True, "source": "derived"},
    )
    associations.associate(
        principal, role="rolls_up_to", from_type="investment_account",
        from_id=account_id, to_type="organization", to_id=str(gp["id"]),
    )
    return 1
