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

from server.core import identity, permissions, registry, repository
from server.core.registry import (
    AssociationRole,
    EntitySpec,
    FieldSpec,
    register,
    register_role,
)

MODULE = "funds"

# investment_accounts.account_type -- closed vocabulary, same anti-drift
# discipline as investor_portal's ASSET_CLASSES/STAGES.
ACCOUNT_TYPES = (
    "individual", "joint", "trust", "llc", "lp", "corporation", "ira", "daf",
    "foundation", "endowment", "spv", "other",
)
ACCOUNT_STATUSES = ("prospect", "onboarding", "active", "restricted", "closed")

# core.gp_roles, seeded per-org at creation (see _seed_gp_roles below) --
# describes what someone actually does at a GP firm (Managing Partner, CFO,
# ...). Deliberately a plain admin-configurable reference list, the same
# shape as investor_portal's investor_categories, rather than a fixed enum:
# the user was explicit this must stay pure description and never get wired
# to portal/permission logic -- that's a separate, later milestone, and
# `principal_of`'s `attributes.gp_role_key` (a soft reference, see
# install() below) can't leak into it because core.associations has no
# owner_id and is structurally excluded from visibility_predicate().
_SEED_GP_ROLES = (
    {"key": "managing_partner", "label": "Managing Partner", "sort_order": 0},
    {"key": "general_partner", "label": "General Partner", "sort_order": 1},
    {"key": "cfo", "label": "CFO", "sort_order": 2},
    {"key": "coo", "label": "COO", "sort_order": 3},
    {"key": "cio", "label": "CIO", "sort_order": 4},
    {"key": "investor_relations", "label": "Investor Relations", "sort_order": 5},
    {"key": "analyst", "label": "Analyst", "sort_order": 6},
    {"key": "operations", "label": "Operations", "sort_order": 7},
    {"key": "other", "label": "Other", "sort_order": 8},
)

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
    # The investment vehicle layer: the actual thing that commits capital --
    # a trust, an LLC, an IRA, a personal account, an SPV. `entity_org_id`
    # mirrors `funds.entity_org_id` (the account's own legal entity, when it
    # has one); `person_id` is the direct-personal-account case. Both
    # nullable, and normally exactly one is set -- same shape as
    # `commitments.investor_org_id`/`investor_person_id`, which today is
    # equally unenforced (no CHECK, no validator on either), not a stronger
    # precedent this table is falling short of. Not a CHECK here either,
    # since "exactly one of two nullable
    # FKs" is the same shape already accepted there without one.
    #
    # Declared BEFORE core.commitments (which gets an FK to this table in
    # Phase E) -- table creation follows dict insertion order, and an FK
    # must point at an already-created table (the same backwards-FK
    # convention modules/investor_portal's pathway_vehicles -> core.funds
    # already established).
    "core.investment_accounts": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "name": "text NOT NULL DEFAULT ''",
        "entity_org_id": "uuid REFERENCES core.organizations(id) ON DELETE SET NULL",
        "person_id": "uuid REFERENCES core.persons(id) ON DELETE SET NULL",
        "account_type": (
            "text NOT NULL DEFAULT 'other' CHECK (account_type IN ("
            "'individual','joint','trust','llc','lp','corporation','ira',"
            "'daf','foundation','endowment','spv','other'))"
        ),
        # ISO-3166-1 alpha-2 (server/core/identity.py's normalize_country),
        # same representation and same validate-and-uppercase-on-write
        # behavior as Phase A's organization/person country columns --
        # via this entity's own FieldSpec.normalize below, the generic
        # mechanism repository.py's write path applies to any field that
        # declares one, so this module never has to teach core the name
        # `investment_account` (R6) to get the same canonicalization.
        "domicile_country": "text NOT NULL DEFAULT ''",
        "base_currency": "text NOT NULL DEFAULT 'USD'",
        "status": (
            "text NOT NULL DEFAULT 'prospect' CHECK (status IN ("
            "'prospect','onboarding','active','restricted','closed'))"
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
        # Phase E: the investment vehicle that actually made this
        # commitment. Added alongside the two columns below rather than
        # replacing them -- existing rows need `modules/funds/backfill.py`
        # run once before every commitment has one, and dropping
        # investor_org_id/investor_person_id before every deployment has
        # backfilled is a destructive step for a later pass, not this one.
        "investment_account_id": (
            "uuid REFERENCES core.investment_accounts(id) ON DELETE SET NULL"
        ),
        # An institutional LP is an entity with contacts hanging off it; an
        # individual LP is a person. Both commit, so both are expressible.
        # Superseded by investment_account_id above once a commitment has
        # been backfilled; kept until every existing row has one.
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
    # Admin-configurable "what does this person actually do at the GP" --
    # same shape as investor_portal's core.investor_categories, seeded per-
    # org (see _seed_gp_roles). Referenced only as a soft key
    # (associations.attributes.gp_role_key on `principal_of`), never a real
    # FK from core.associations -- see install()'s comment on that role.
    "core.gp_roles": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "key": "text NOT NULL DEFAULT ''",
        "label": "text NOT NULL DEFAULT ''",
        "is_enabled": "boolean NOT NULL DEFAULT true",
        "sort_order": "integer NOT NULL DEFAULT 0",
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
    "CREATE INDEX IF NOT EXISTS ix_commitments_org_investment_account "
    "ON core.commitments (org_id, investment_account_id)",
    "CREATE INDEX IF NOT EXISTS ix_funds_custom_gin "
    "ON core.funds USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_commitments_custom_gin "
    "ON core.commitments USING gin (custom jsonb_path_ops)",
    "CREATE INDEX IF NOT EXISTS ix_investment_accounts_org_updated "
    "ON core.investment_accounts (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_investment_accounts_org_owner "
    "ON core.investment_accounts (org_id, owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_investment_accounts_org_entity "
    "ON core.investment_accounts (org_id, entity_org_id)",
    "CREATE INDEX IF NOT EXISTS ix_investment_accounts_org_person "
    "ON core.investment_accounts (org_id, person_id)",
    "CREATE INDEX IF NOT EXISTS ix_investment_accounts_custom_gin "
    "ON core.investment_accounts USING gin (custom jsonb_path_ops)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_gp_roles_org_key "
    "ON core.gp_roles (org_id, key)",
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


def _seed_gp_roles(org_id: str) -> None:
    """Called once, right after an org is created (see
    `registry.register_org_seed`) -- same pattern as investor_portal's
    `_seed_investor_categories`. All 9 seeded enabled: unlike investor
    categories there is no compliance reason to ship any of these disabled
    by default."""
    principal = permissions.system_principal(org_id, "seed default GP roles")
    for row in _SEED_GP_ROLES:
        repository.create(principal, "gp_role", dict(row))


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
            "investment_account_id": FieldSpec("investment_account_id", "uuid",
                                               column="investment_account_id"),
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

    register(EntitySpec(
        name="investment_account", table="core.investment_accounts",
        label="Investment accounts", module=MODULE,
        label_field="name", searchable=("name",),
        fields=_spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "entity_org_id": FieldSpec("entity_org_id", "uuid", column="entity_org_id"),
            "person_id": FieldSpec("person_id", "uuid", column="person_id"),
            "account_type": FieldSpec("account_type", "select", column="account_type",
                                      options=ACCOUNT_TYPES),
            "domicile_country": FieldSpec("domicile_country", "text",
                                          column="domicile_country",
                                          normalize=identity.normalize_country),
            "base_currency": FieldSpec("base_currency", "text", column="base_currency"),
            "status": FieldSpec("status", "select", column="status",
                                options=ACCOUNT_STATUSES),
        }),
    ))

    register(EntitySpec(
        name="gp_role", table="core.gp_roles", label="GP roles", module=MODULE,
        label_field="label", admin_only=True, supports_custom_fields=False,
        default_sort=(("sort_order", "asc"),),
        fields=_spine({
            "key": FieldSpec("key", "text", column="key", required=True),
            "label": FieldSpec("label", "text", column="label", required=True),
            "is_enabled": FieldSpec("is_enabled", "boolean", column="is_enabled"),
            "sort_order": FieldSpec("sort_order", "number", column="sort_order"),
        }),
    ))

    # The vertical's relationship vocabulary. Core knows none of these words.
    # Phase E: `investment_account` added to `from_types` alongside
    # organization/person -- a commitment is properly made BY an account,
    # and `lp_in` needs to accept one as an LP the same way it already
    # accepts a bare org or person (pre-Phase-B commitments that predate
    # investment_account keep working unchanged; this only widens what a
    # NEW `lp_in` edge may name on the "from" side).
    register_role(AssociationRole(
        "lp_in", ("organization", "person", "investment_account"), ("fund",),
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
    # The investment-relationship rollup -- deliberately NOT legal-entity-
    # based, unlike core's `owned_by` (Goldman's advisory and investment-
    # advisory arms are separate legal entities but can roll up under one
    # Investment GP relationship that ignores that structure entirely).
    # `from` covers both an org-level GP nesting under a bigger GP AND the
    # leaf edge from an investment_account up to its GP, in one traversable
    # dimension -- same child-to-parent convention as `owned_by`, same
    # traversal mechanism (server/core/hierarchy.py), different real-world
    # concept.
    register_role(AssociationRole(
        "rolls_up_to", ("organization", "investment_account"), ("organization",),
        inverse_label="rolls up from", hierarchical=True, module=MODULE))

    # Roles at the GP level (person -> organization). `principal_of` is
    # deliberately the only one that carries a functional title, via
    # `attributes.gp_role_key` -- a soft reference to `core.gp_roles.key`,
    # not a real FK column on `core.associations`: adding one there for one
    # vertical's need would put vertical vocabulary on a generic core table
    # (R6), the same reason `attributes` exists at all ("role-specific
    # extras that don't deserve columns... title, board seat type,
    # ownership %", docs/VERTICAL-ASSET-MANAGEMENT.md). This is pure
    # description of who does what -- it is NOT wired to portal access or
    # any permission decision, and structurally can't be: associations have
    # no `owner_id` and are excluded from `visibility_predicate()`.
    register_role(AssociationRole(
        "principal_of", ("person",), ("organization",),
        inverse_label="principals", module=MODULE))
    # No separate `beneficial_owner_of` role: core's `owned_by` already
    # covers a person owning an organization directly (its own docstring
    # names exactly this GP-with-no-corporate-parent case), including the
    # hierarchy traversal (server/core/hierarchy.py's cycle-checked
    # recursive CTE) a second, non-hierarchical role would lose. Beneficial
    # ownership is `owned_by` with `attributes.ownership_type="beneficial"`
    # (and, later, a percentage) when that AML/KYC distinction from
    # ordinary equity ownership needs to be recorded -- the same
    # attributes-carries-the-extras convention `principal_of`'s
    # `gp_role_key` uses above, not a new role.
    #
    # Spans both levels -- the same person can be an authorized signer at
    # the GP or on a specific account, and it's one concept, not two.
    register_role(AssociationRole(
        "authorized_signer_for", ("person",), ("organization", "investment_account"),
        inverse_label="authorized signers", module=MODULE))

    # Roles at the account level (person -> investment_account).
    register_role(AssociationRole(
        "account_holder_of", ("person",), ("investment_account",),
        inverse_label="account holders", module=MODULE))
    register_role(AssociationRole(
        "trustee_of", ("person",), ("investment_account",),
        inverse_label="trustees", module=MODULE))
    register_role(AssociationRole(
        "beneficiary_of", ("person",), ("investment_account",),
        inverse_label="beneficiaries", module=MODULE))

    registry.register_org_seed(_seed_gp_roles, module=MODULE)
