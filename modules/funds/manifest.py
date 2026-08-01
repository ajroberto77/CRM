"""The asset-management vertical, as a module.

Its purpose is to prove the architecture: adding a domain must need **a manifest
and its own tables, and no change to anything under `server/core/`** (R6). If
this file ever has to reach into core, the seam is in the wrong place and that
is the finding.

## What is deliberately absent

Capital transactions, positions, IRR/MOIC/DPI, documents, KYC and the LP portal.
The bar here is "who committed how much to which fund", not a fund
administration system — see `docs/VERTICAL-ASSET-MANAGEMENT.md` for why fund
accounting stays out of scope entirely.

## The rule this module embodies

**"Investor" is not an entity type.** There is no `investors` table here.
An investor is an ordinary `organization` (or `person`) carrying an `lp_in`
association to a fund — because the same legal entity is routinely an LP, a
co-investor and a portfolio company at once, and separate tables would split it
into duplicates with separate interaction histories.

Funds themselves are registered as an entity, but a fund's *legal identity* is
an `organizations` row referenced by `entity_org_id`. That is what makes another
GP's fund the same shape as your own: it sits in the relationship graph, appears
as a co-investor, and accumulates its own interactions, at no extra cost.
"""
from __future__ import annotations

from server.core.registry import (
    AssociationRole,
    EntitySpec,
    FieldSpec,
    register,
    register_role,
)

MODULE = "funds"

# Module-owned tables, merged into the schema by db/schema.py's ordinary
# migration path. Declared here rather than in core so the vertical is
# removable, and so core carries no fund-shaped column.
TABLES: dict[str, dict[str, str]] = {
    "core.funds": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        # A fund IS a legal entity. Pointing at organizations is what lets a
        # competitor's fund be tracked with exactly this shape.
        "entity_org_id": "uuid REFERENCES core.organizations(id) ON DELETE SET NULL",
        "name": "text NOT NULL DEFAULT ''",
        "vintage_year": "integer",
        "strategy": "text NOT NULL DEFAULT ''",
        "currency": "text NOT NULL DEFAULT 'USD'",
        "target_size": "numeric(18,2)",
        "hard_cap": "numeric(18,2)",
        "first_close_at": "date",
        "final_close_at": "date",
        "status": (
            "text NOT NULL DEFAULT 'raising' "
            "CHECK (status IN ('raising','investing','harvesting','closed'))"
        ),
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    "core.commitments": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "fund_id": "uuid REFERENCES core.funds(id) ON DELETE CASCADE",
        # An institutional LP is an entity with contacts hanging off it; an
        # individual LP is a person. Both commit, so both are expressible.
        "investor_org_id": "uuid REFERENCES core.organizations(id) ON DELETE SET NULL",
        "investor_person_id": "uuid REFERENCES core.persons(id) ON DELETE SET NULL",
        "amount": "numeric(18,2)",
        "currency": "text NOT NULL DEFAULT 'USD'",
        "committed_at": "date",
        "status": (
            "text NOT NULL DEFAULT 'soft' "
            "CHECK (status IN ('soft','signed','closed','withdrawn'))"
        ),
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
}

INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS ix_funds_org_updated "
    "ON core.funds (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_funds_org_owner ON core.funds (org_id, owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_funds_org_entity "
    "ON core.funds (org_id, entity_org_id)",
    "CREATE INDEX IF NOT EXISTS ix_commitments_org_updated "
    "ON core.commitments (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_commitments_org_owner "
    "ON core.commitments (org_id, owner_id)",
    # The two directions of "who invested in which fund".
    "CREATE INDEX IF NOT EXISTS ix_commitments_org_fund "
    "ON core.commitments (org_id, fund_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_commitments_org_investor "
    "ON core.commitments (org_id, investor_org_id)",
    "CREATE INDEX IF NOT EXISTS ix_funds_custom_gin "
    "ON core.funds USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_commitments_custom_gin "
    "ON core.commitments USING gin (custom jsonb_path_ops)",
)


def _spine(extra: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
    base = {
        "id": FieldSpec("id", "uuid", column="id", writable=False),
        "owner_id": FieldSpec("owner_id", "uuid", column="owner_id", write_level="team"),
        "created_at": FieldSpec("created_at", "datetime", column="created_at",
                                writable=False),
        "updated_at": FieldSpec("updated_at", "datetime", column="updated_at",
                                writable=False),
    }
    base.update(extra)
    return base


def install() -> None:
    """Register the module. Entities and roles go through exactly the same
    functions core uses -- that symmetry is the point."""
    register(EntitySpec(
        name="fund", table="core.funds", label="Funds", module=MODULE,
        label_field="name", searchable=("name", "strategy"),
        fields=_spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "entity_org_id": FieldSpec("entity_org_id", "uuid", column="entity_org_id"),
            "vintage_year": FieldSpec("vintage_year", "number", column="vintage_year"),
            "strategy": FieldSpec("strategy", "text", column="strategy"),
            "currency": FieldSpec("currency", "text", column="currency"),
            "target_size": FieldSpec("target_size", "currency", column="target_size"),
            "hard_cap": FieldSpec("hard_cap", "currency", column="hard_cap"),
            "first_close_at": FieldSpec("first_close_at", "date", column="first_close_at"),
            "final_close_at": FieldSpec("final_close_at", "date", column="final_close_at"),
            "status": FieldSpec("status", "select", column="status",
                                options=("raising", "investing", "harvesting", "closed")),
        }),
    ))

    register(EntitySpec(
        name="commitment", table="core.commitments", label="Commitments",
        module=MODULE, label_field="amount",
        default_sort=(("committed_at", "desc"),),
        fields=_spine({
            "fund_id": FieldSpec("fund_id", "uuid", column="fund_id", required=True),
            "investor_org_id": FieldSpec("investor_org_id", "uuid",
                                         column="investor_org_id"),
            "investor_person_id": FieldSpec("investor_person_id", "uuid",
                                            column="investor_person_id"),
            "amount": FieldSpec("amount", "currency", column="amount"),
            "currency": FieldSpec("currency", "text", column="currency"),
            "committed_at": FieldSpec("committed_at", "date", column="committed_at"),
            "status": FieldSpec("status", "select", column="status",
                                options=("soft", "signed", "closed", "withdrawn")),
        }),
    ))

    # The vertical's relationship vocabulary. Core knows none of these words.
    register_role(AssociationRole(
        "lp_in", ("organization", "person"), ("fund",),
        inverse_label="investors", module=MODULE))
    register_role(AssociationRole(
        "gp_of", ("organization", "person"), ("fund",),
        inverse_label="general partners", module=MODULE))
    register_role(AssociationRole(
        "portfolio_of", ("organization",), ("fund",),
        inverse_label="portfolio", module=MODULE))
    # Symmetric: canonicalized on write so A/B and B/A cannot both exist and
    # every co-investment is not counted twice.
    register_role(AssociationRole(
        "co_investor_in", ("organization",), ("organization",),
        inverse_label="co-investors", symmetric=True, module=MODULE))
    register_role(AssociationRole(
        "lender_to", ("organization",), ("organization",),
        inverse_label="lenders", module=MODULE))
    register_role(AssociationRole(
        "acquirer_of", ("organization",), ("organization",),
        inverse_label="acquired by", module=MODULE))
