"""The investor-portal module: write-time rules (M9d) and, from M9a on, the
investor-classification schema itself.

## M9d — the write-time rule

Registers one validator: **a commitment cannot close without an executed
subscription agreement.** This is the concrete case
`docs/INVESTOR-PORTAL.md` names for the write-time validator mechanism in
`server/core/registry.py`/`server/core/repository.py` -- the gate M1's
event bus could not provide, because its subscribers run after commit and
cannot block a write.

`commitment` is a `modules/funds` entity; `document` is core. This module
depends on both, in the same one-way direction already established for
`pathway_vehicles` -> `core.funds` -- neither `server/core/` nor
`modules/funds` gains any awareness that `investor_portal` exists.

## Data model Phase D — AML/tax document gating on investment_account

A second validator, same shape and same reasoning as M9d's: an
`investment_account` (a `modules/funds` entity, added in Phase B) cannot
reach `status='active'` without its AML/KYC and tax-withholding documents
on file, executed and unexpired. Lives here rather than in
`modules/funds/manifest.py` for the same reason `_validate_commitment_closing`
does -- this is investor-onboarding-compliance logic, this module's whole
reason to exist, not "funds and commitments exist" logic.

Registered for BOTH `create` and `update` (M9d's gate only covers
`update`) -- an account could otherwise be created pre-set to `active` in
one write, skipping the transition check entirely. In practice this can
never actually satisfy the gate: a compliance document must reference an
already-existing `subject_id`, so a direct create-as-active request is
correctly rejected for having zero documents on file, not specially
detected.

## M9a — investor classification

"Investor" is still not an entity type (see `modules/funds/manifest.py`'s
same rule): an investor is an ordinary `organization`/`person` carrying
`lp_in` associations. What M9a adds attaches to the existing record --
`investor_profile` (status/accreditation/relationship), `investor_mandate`
(what they want, derived from a questionnaire response, correctable by
hand) -- plus the admin-configurable reference data both draw from
(`investor_category`, `investment_pathway`, `pathway_vehicle`) and the
versioned questionnaire itself (`questionnaire`, `questionnaire_response`).

No new `AssociationRole` is needed here; classification is a property of the
existing organization/person record, not a new relationship.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Optional

from server.core import events, permissions, registry, repository, settings as core_settings
from server.core.registry import EntitySpec, FieldSpec, register

MODULE = "investor_portal"

# The `kind` this module uses on core.documents to mean "the document that,
# once executed, satisfies a commitment's closing gate." Free text at the
# schema level (documents.kind has no CHECK constraint -- it is generic); this
# constant is the one place that vocabulary is spelled, so it is never typo'd
# differently in two places (R1).
SUBSCRIPTION_AGREEMENT_KIND = "subscription_agreement"

# The document kinds Phase D's account-activation gate (below) checks for,
# plus two more that exist as named vocabulary without being part of that
# specific gate: PROOF_OF_ADDRESS_KIND is typically evidence bundled into
# an AML_KYC_KIND packet rather than a separately-gated document, and
# ACCREDITATION_EVIDENCE_KIND backs `investor_profile.accreditation_method`
# ='verified' -- a different, already-existing workflow, not this one.
W9_KIND = "w9"
W8BEN_KIND = "w8ben"
W8BENE_KIND = "w8bene"
AML_KYC_KIND = "aml_kyc"
PROOF_OF_ADDRESS_KIND = "proof_of_address"
ACCREDITATION_EVIDENCE_KIND = "accreditation_evidence"

# The closed vocabulary for investor_mandate.asset_classes -- free text drifts
# and silently stops matching, the same failure Cal hit with event categories
# (docs/INVESTOR-PORTAL.md).
ASSET_CLASSES = (
    "venture", "growth_equity", "private_equity", "public_equity", "activism",
    "private_credit", "venture_debt", "real_assets", "infrastructure",
    "secondaries", "fund_of_funds", "crypto", "other",
)

# Not specified in docs/INVESTOR-PORTAL.md's schema sketch -- a reasonable
# closed set for the "stage" dimension, applying the same anti-drift
# discipline as ASSET_CLASSES rather than leaving it free text.
STAGES = ("seed", "series_a", "series_b", "series_c_plus", "growth", "late_stage", "other")

LIQUIDITY_NEEDS = ("illiquid_ok", "moderate", "needs_liquidity")

# core.investor_categories, seeded per-org at creation (see _seed_investor_categories
# below) -- two enabled today, four present but disabled so turning one on later
# is a data change, never a migration.
_SEED_CATEGORIES = (
    {"key": "accredited_individual", "label": "Accredited individual",
     "requires_verification": False, "is_enabled": True, "sort_order": 0},
    {"key": "accredited_entity", "label": "Accredited entity",
     "requires_verification": False, "is_enabled": True, "sort_order": 1},
    {"key": "qualified_purchaser", "label": "Qualified purchaser",
     "requires_verification": True, "is_enabled": False, "sort_order": 2},
    {"key": "qualified_client", "label": "Qualified client",
     "requires_verification": True, "is_enabled": False, "sort_order": 3},
    {"key": "institutional", "label": "Institutional",
     "requires_verification": True, "is_enabled": False, "sort_order": 4},
    {"key": "non_accredited", "label": "Non-accredited",
     "requires_verification": False, "is_enabled": False, "sort_order": 5},
)

# Module-owned tables, merged into the schema by db/schema.py's ordinary
# migration path -- same convention as modules/funds: still under the `core.`
# Postgres schema (R6 is about vocabulary in server/core/ Python, not the
# Postgres namespace), declared here so core carries no investor-shaped column.
# Insertion order is creation order; FKs point backwards, matching
# modules/funds' own convention and the fact that `funds` installs first (see
# config.get_enabled_modules()'s default order) so pathway_vehicles.fund_id
# resolves against an already-created core.funds.
TABLES: dict[str, dict[str, str]] = {
    "core.investor_categories": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "key": "text NOT NULL DEFAULT ''",
        "label": "text NOT NULL DEFAULT ''",
        "requires_verification": "boolean NOT NULL DEFAULT false",
        "is_enabled": "boolean NOT NULL DEFAULT true",
        "sort_order": "integer NOT NULL DEFAULT 0",
        # Every registered entity carries this column regardless of
        # `supports_custom_fields` -- that flag gates the custom-field
        # *registry*, not whether the column exists (core.custom_fields
        # itself, admin_only and supports_custom_fields=False, still has
        # one -- see server/db/schema.py's comment on it).
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    "core.investment_pathways": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "key": "text NOT NULL DEFAULT ''",
        "label": "text NOT NULL DEFAULT ''",
        "description": "text NOT NULL DEFAULT ''",
        "default_min_check": "numeric(18,2)",
        "default_currency": "text NOT NULL DEFAULT 'USD'",
        "is_enabled": "boolean NOT NULL DEFAULT true",
        "sort_order": "integer NOT NULL DEFAULT 0",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    # The one table in this module that references core.funds. The dependency
    # runs one way only -- modules/funds gains no column, no table, and no
    # awareness that investor_portal exists.
    "core.pathway_vehicles": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "pathway_id": "uuid REFERENCES core.investment_pathways(id) ON DELETE CASCADE",
        "fund_id": "uuid REFERENCES core.funds(id) ON DELETE CASCADE",
        "added_at": "date",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    "core.investor_profiles": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        # Polymorphic subject -- organization or person -- same pattern as
        # core.tasks/core.notes/core.documents. No FK: the target table
        # varies.
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "category_id": "uuid REFERENCES core.investor_categories(id) ON DELETE SET NULL",
        "pathway_id": "uuid REFERENCES core.investment_pathways(id) ON DELETE SET NULL",
        "status": (
            "text NOT NULL DEFAULT 'prospect' CHECK (status IN ("
            "'prospect','questionnaire_sent','questionnaire_complete',"
            "'self_accredited','verified','active','lapsed','declined'))"
        ),
        # 506(b): self-certification does not satisfy 506(c)'s verified-
        # accreditation requirement, so this defaults to self_certified with
        # no verification vendor in v1. Stays multi-valued because a future
        # category (a qualified purchaser for a 3(c)(7) vehicle) may need
        # real verification even while the general population self-certifies.
        "accreditation_method": (
            "text NOT NULL DEFAULT 'self_certified' "
            "CHECK (accreditation_method IN ('self_certified','verified'))"
        ),
        "accredited_at": "date",
        "accreditation_expires_at": "date",
        "verified_by": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        # The 506(b) evidence trail: how long the relationship predates any
        # offering shown to this investor.
        "relationship_since": "date",
        "check_min": "numeric(18,2)",
        "check_max": "numeric(18,2)",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    "core.questionnaires": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "name": "text NOT NULL DEFAULT ''",
        "version": "integer NOT NULL DEFAULT 1",
        # The question list. See _derive_mandate()'s docstring for the exact
        # per-question shape ({key, type, options, maps_to}).
        "question_schema": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "published_at": "timestamptz",
        "retired_at": "timestamptz",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    "core.questionnaire_responses": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "questionnaire_id": "uuid REFERENCES core.questionnaires(id) ON DELETE SET NULL",
        # Denormalized on purpose: publishing v2 must never change what a v1
        # response meant (docs/INVESTOR-PORTAL.md).
        "questionnaire_version": "integer NOT NULL DEFAULT 1",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        "answers": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "submitted_at": "timestamptz",
        "submitted_by": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
    "core.investor_mandates": {
        "id": "uuid PRIMARY KEY DEFAULT gen_random_uuid()",
        "org_id": "uuid NOT NULL REFERENCES core.orgs(id) ON DELETE CASCADE",
        "owner_id": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "subject_type": "text NOT NULL DEFAULT ''",
        "subject_id": "uuid",
        # 'response' = deterministically derived from a questionnaire_response
        # (what this module builds). 'ai' is reserved for when an actual
        # LLM-derivation path exists (M3+) -- not built here; see the module
        # docstring on the human>sync>ai precedence gap this deliberately
        # does not need yet.
        "source": (
            "text NOT NULL DEFAULT 'human' CHECK (source IN ('response','human','ai'))"
        ),
        "confidence": "numeric(4,3)",
        "asset_classes": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "stages": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "sectors": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "geographies": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "check_min": "numeric(18,2)",
        "check_max": "numeric(18,2)",
        "currency": "text NOT NULL DEFAULT 'USD'",
        "hold_horizon_years": "integer",
        "liquidity_need": (
            "text CHECK (liquidity_need IN ('illiquid_ok','moderate','needs_liquidity'))"
        ),
        "esg_constraints": "jsonb NOT NULL DEFAULT '[]'::jsonb",
        "derived_from_response_id": (
            "uuid REFERENCES core.questionnaire_responses(id) ON DELETE SET NULL"
        ),
        "reviewed_by": "uuid REFERENCES core.users(id) ON DELETE SET NULL",
        "reviewed_at": "timestamptz",
        "custom": "jsonb NOT NULL DEFAULT '{}'::jsonb",
        "created_at": "timestamptz NOT NULL DEFAULT now()",
        "updated_at": "timestamptz NOT NULL DEFAULT now()",
    },
}

INDEXES: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_investor_categories_org_key "
    "ON core.investor_categories (org_id, key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_investment_pathways_org_key "
    "ON core.investment_pathways (org_id, key)",
    "CREATE INDEX IF NOT EXISTS ix_investment_pathways_custom_gin "
    "ON core.investment_pathways USING gin (custom jsonb_path_ops)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_pathway_vehicles_org_pathway_fund "
    "ON core.pathway_vehicles (org_id, pathway_id, fund_id)",
    "CREATE INDEX IF NOT EXISTS ix_pathway_vehicles_org_fund "
    "ON core.pathway_vehicles (org_id, fund_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_investor_profiles_org_subject "
    "ON core.investor_profiles (org_id, subject_type, subject_id)",
    "CREATE INDEX IF NOT EXISTS ix_investor_profiles_org_updated "
    "ON core.investor_profiles (org_id, updated_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_investor_profiles_custom_gin "
    "ON core.investor_profiles USING gin (custom jsonb_path_ops)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_questionnaires_org_name_version "
    "ON core.questionnaires (org_id, name, version)",
    "CREATE INDEX IF NOT EXISTS ix_questionnaire_responses_org_subject "
    "ON core.questionnaire_responses (org_id, subject_type, subject_id)",
    "CREATE INDEX IF NOT EXISTS ix_questionnaire_responses_org_questionnaire "
    "ON core.questionnaire_responses (org_id, questionnaire_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_investor_mandates_org_subject "
    "ON core.investor_mandates (org_id, subject_type, subject_id)",
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


class SubscriptionAgreementMissing(repository.ValidationError):
    """Raised when a commitment tries to close without an executed
    subscription agreement on file. A ValidationError subclass so it is
    caught by anything already handling that class, while still being
    identifiable to a caller that wants to react specifically."""


def _current_document_exists(
    principal: permissions.Principal, subject_type: str, subject_id: str, kind: str,
    *, require_unexpired: bool = False,
) -> bool:
    """True iff `subject` has at least one document of `kind`, executed and
    (when `require_unexpired`) not past its `valid_until`. Shared by both
    write-time document gates in this module -- M9d's commitment-closing
    check below and Phase D's account-activation check further down -- so
    "does an executed document of kind X exist for subject Y" lives in
    exactly one place (R1).

    The unexpired check is expressed IN the filter tree (an `or` of
    `valid_until IS NULL` and `valid_until >= today`, both already
    supported by `server/core/query.py`'s filter DSL), not filtered in
    Python after the fact -- so `limit=1` alone is correct regardless of
    how many executed documents of this kind a subject has accumulated,
    with no arbitrary page-size ceiling to silently outgrow.
    """
    conditions: list[dict[str, Any]] = [
        {"field": "subject_type", "op": "eq", "value": subject_type},
        {"field": "subject_id", "op": "eq", "value": subject_id},
        {"field": "kind", "op": "eq", "value": kind},
        {"field": "status", "op": "eq", "value": "executed"},
    ]
    if require_unexpired:
        conditions.append({"or": [
            {"field": "valid_until", "op": "is_null"},
            {"field": "valid_until", "op": "gte", "value": date.today().isoformat()},
        ]})
    result = repository.list_records(
        principal, "document", filters={"and": conditions}, limit=1,
    )
    return result["total"] > 0


def _validate_commitment_closing(ctx: registry.ValidationContext) -> None:
    """The gate. Fires only on the transition INTO 'closed' -- a commitment
    that is already closed being edited for an unrelated reason (a note, a
    currency correction) does not re-check, and a commitment moving between
    any other two statuses is untouched by this rule entirely.
    """
    if ctx.action != "update":
        return
    before_status = (ctx.before or {}).get("status")
    after_status = (ctx.after or {}).get("status")
    if after_status != "closed" or before_status == "closed":
        return

    if not _current_document_exists(
        ctx.principal, "commitment", ctx.record_id, SUBSCRIPTION_AGREEMENT_KIND
    ):
        raise SubscriptionAgreementMissing(
            "this commitment cannot close: no executed subscription agreement "
            "is on file for it"
        )


class AccountActivationBlocked(repository.ValidationError):
    """Raised when an investment_account tries to reach 'active' without its
    required AML/KYC and tax-withholding documents on file, executed and
    unexpired."""


def _account_activation_gate_enabled(org_id: str) -> bool:
    """Strictness is a per-org `core.settings` key under the "compliance"
    section, defaulting to enforcing -- destructive/consequential paths
    default off, but a compliance GATE defaults ON (CLAUDE.md safety rule 9
    is about defaulting a destructive action off; refusing to gate an
    activation by default would be the same mistake in the other
    direction)."""
    values = core_settings.get_settings(org_id, "compliance")
    return bool(values.get("enforce_account_activation_gate", True))


def _required_document_kinds(
    principal: permissions.Principal, account: dict[str, Any]
) -> Optional[list[str]]:
    """AML/KYC is always required. The tax-withholding form depends on the
    account's tax country: the held person's `tax_residence_country` for a
    person-held account, else the account's own `domicile_country` (an
    entity is taxed on its own formation jurisdiction for the purpose of
    picking a form, not looked through to its underlying owners).

    Returns None -- "not determinable yet" -- when neither country is on
    file, so the caller BLOCKS rather than silently requiring only AML/KYC:
    a null category never satisfies a gate (CLAUDE.md safety rule 8).
    """
    tax_country = None
    person_id = account.get("person_id")
    if person_id:
        person = repository.get_record(principal, "person", str(person_id))
        tax_country = person.get("tax_residence_country") or None
    if not tax_country:
        tax_country = account.get("domicile_country") or None
    if not tax_country:
        return None

    kinds = [AML_KYC_KIND]
    if tax_country == "US":
        kinds.append(W9_KIND)
    else:
        kinds.append(W8BENE_KIND if account.get("entity_org_id") else W8BEN_KIND)
    return kinds


def _validate_investment_account_activation(ctx: registry.ValidationContext) -> None:
    """The gate. Fires on the transition INTO 'active' -- via `create` OR
    `update` (see the module docstring for why this covers `create` too,
    unlike `_validate_commitment_closing` above). An account already active
    being edited for an unrelated reason does not recheck.
    """
    after = ctx.after
    if not after or after.get("status") != "active":
        return
    if (ctx.before or {}).get("status") == "active":
        return
    if not _account_activation_gate_enabled(ctx.principal.org_id):
        return

    required = _required_document_kinds(ctx.principal, after)
    if required is None:
        raise AccountActivationBlocked(
            "this account cannot activate: no tax residence or domicile "
            "country on file to determine the required tax forms"
        )

    for kind in required:
        if not _current_document_exists(
            ctx.principal, "investment_account", ctx.record_id, kind,
            require_unexpired=True,
        ):
            raise AccountActivationBlocked(
                f"this account cannot activate: no executed, unexpired "
                f"{kind!r} document is on file for it"
            )


def _seed_investor_categories(org_id: str) -> None:
    """Called once, right after an org is created (see
    `registry.register_org_seed`). Creates the 6 rows -- 2 enabled, 4 present
    but disabled -- through the ordinary generic CRUD path with a system
    principal, not hand-written SQL, so a category is subject to exactly the
    same validation as one an admin creates by hand later.
    """
    principal = permissions.system_principal(org_id, "seed default investor categories")
    for row in _SEED_CATEGORIES:
        repository.create(principal, "investor_category", dict(row))


def _derive_mandate(event: events.RecordEvent) -> None:
    """On a new questionnaire_response, upsert the subject's investor_mandate
    from it. Deterministic, not AI-derived (see module docstring) --
    `source='response'`, `confidence=1.0`.

    A published questionnaire's `question_schema` is a JSON list of question
    objects: `{"key": ..., "type": ..., "options": [...], "maps_to": ...}`.
    `maps_to` names the investor_mandate field this question's answer feeds;
    a question with no `maps_to` is informational only. A question the
    investor skipped has no key in `answers`, so that mandate field is left
    unset -- null never auto-matches, the same "fails safe" rule as
    everywhere else in the platform.

    Re-submitting a later response for the same subject upserts the SAME
    mandate row (updates in place) rather than appending a second one --
    "re-running classification after a questionnaire change is a backfill,
    not a data-loss event" (docs/INVESTOR-PORTAL.md).
    """
    response = event.after
    if not response:
        return
    principal = permissions.system_principal(
        event.org_id, "derive investor mandate from questionnaire response"
    )

    questionnaire = repository.get_record(principal, "questionnaire", str(response["questionnaire_id"]))
    answers = response.get("answers") or {}
    mapped: dict[str, Any] = {}
    for question in questionnaire.get("question_schema") or []:
        maps_to = question.get("maps_to")
        key = question.get("key")
        if not maps_to or key not in answers:
            continue
        mapped[maps_to] = answers[key]

    mapped["source"] = "response"
    mapped["confidence"] = 1.0
    mapped["derived_from_response_id"] = str(response["id"])

    existing = repository.list_records(
        principal, "investor_mandate",
        filters={"and": [
            {"field": "subject_type", "op": "eq", "value": response["subject_type"]},
            {"field": "subject_id", "op": "eq", "value": str(response["subject_id"])},
        ]},
        limit=1,
    )
    if existing["total"] > 0:
        repository.update(principal, "investor_mandate", str(existing["records"][0]["id"]), mapped)
    else:
        mapped["subject_type"] = response["subject_type"]
        mapped["subject_id"] = str(response["subject_id"])
        repository.create(principal, "investor_mandate", mapped)


def install() -> None:
    """Register the module. Entities and roles go through exactly the same
    functions core uses -- that symmetry is the point. Idempotent, same as
    modules/funds."""
    register(EntitySpec(
        name="investor_category", table="core.investor_categories",
        label="Investor categories", module=MODULE,
        label_field="label", admin_only=True, supports_custom_fields=False,
        default_sort=(("sort_order", "asc"),),
        fields=_spine({
            "key": FieldSpec("key", "text", column="key", required=True),
            "label": FieldSpec("label", "text", column="label", required=True),
            "requires_verification": FieldSpec(
                "requires_verification", "boolean", column="requires_verification"),
            "is_enabled": FieldSpec("is_enabled", "boolean", column="is_enabled"),
            "sort_order": FieldSpec("sort_order", "number", column="sort_order"),
        }),
    ))

    register(EntitySpec(
        name="investment_pathway", table="core.investment_pathways",
        label="Investment pathways", module=MODULE,
        label_field="label", admin_only=True,
        default_sort=(("sort_order", "asc"),), searchable=("label",),
        fields=_spine({
            "key": FieldSpec("key", "text", column="key", required=True),
            "label": FieldSpec("label", "text", column="label", required=True),
            "description": FieldSpec("description", "text", column="description"),
            "default_min_check": FieldSpec(
                "default_min_check", "currency", column="default_min_check"),
            "default_currency": FieldSpec(
                "default_currency", "text", column="default_currency"),
            "is_enabled": FieldSpec("is_enabled", "boolean", column="is_enabled"),
            "sort_order": FieldSpec("sort_order", "number", column="sort_order"),
        }),
    ))

    register(EntitySpec(
        name="pathway_vehicle", table="core.pathway_vehicles",
        label="Pathway vehicles", module=MODULE,
        label_field="fund_id", admin_only=True, supports_custom_fields=False,
        fields=_spine({
            "pathway_id": FieldSpec("pathway_id", "uuid", column="pathway_id", required=True),
            "fund_id": FieldSpec("fund_id", "uuid", column="fund_id", required=True),
            "added_at": FieldSpec("added_at", "date", column="added_at"),
        }),
    ))

    register(EntitySpec(
        name="investor_profile", table="core.investor_profiles",
        label="Investor profiles", module=MODULE,
        label_field="status",
        fields=_spine({
            "subject_type": FieldSpec("subject_type", "text", column="subject_type",
                                      required=True),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    required=True),
            "category_id": FieldSpec("category_id", "uuid", column="category_id"),
            "pathway_id": FieldSpec("pathway_id", "uuid", column="pathway_id"),
            "status": FieldSpec("status", "select", column="status", options=(
                "prospect", "questionnaire_sent", "questionnaire_complete",
                "self_accredited", "verified", "active", "lapsed", "declined",
            )),
            "accreditation_method": FieldSpec(
                "accreditation_method", "select", column="accreditation_method",
                options=("self_certified", "verified")),
            "accredited_at": FieldSpec("accredited_at", "date", column="accredited_at"),
            "accreditation_expires_at": FieldSpec(
                "accreditation_expires_at", "date", column="accreditation_expires_at"),
            "verified_by": FieldSpec("verified_by", "uuid", column="verified_by"),
            "relationship_since": FieldSpec(
                "relationship_since", "date", column="relationship_since"),
            "check_min": FieldSpec("check_min", "currency", column="check_min"),
            "check_max": FieldSpec("check_max", "currency", column="check_max"),
        }),
    ))

    register(EntitySpec(
        name="questionnaire", table="core.questionnaires",
        label="Questionnaires", module=MODULE,
        label_field="name", admin_only=True, supports_custom_fields=False,
        searchable=("name",),
        fields=_spine({
            "name": FieldSpec("name", "text", column="name", required=True),
            "version": FieldSpec("version", "number", column="version", required=True),
            "question_schema": FieldSpec(
                "question_schema", "jsonb", column="question_schema",
                filterable=False, sortable=False),
            "published_at": FieldSpec(
                "published_at", "datetime", column="published_at"),
            "retired_at": FieldSpec("retired_at", "datetime", column="retired_at"),
        }),
    ))

    register(EntitySpec(
        name="questionnaire_response", table="core.questionnaire_responses",
        label="Questionnaire responses", module=MODULE,
        label_field="submitted_at", supports_custom_fields=False,
        fields=_spine({
            "questionnaire_id": FieldSpec(
                "questionnaire_id", "uuid", column="questionnaire_id", required=True),
            "questionnaire_version": FieldSpec(
                "questionnaire_version", "number", column="questionnaire_version",
                required=True),
            "subject_type": FieldSpec("subject_type", "text", column="subject_type",
                                      required=True),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    required=True),
            "answers": FieldSpec("answers", "jsonb", column="answers",
                                 filterable=False, sortable=False, required=True),
            "submitted_at": FieldSpec(
                "submitted_at", "datetime", column="submitted_at"),
            "submitted_by": FieldSpec(
                "submitted_by", "uuid", column="submitted_by"),
        }),
    ))

    register(EntitySpec(
        name="investor_mandate", table="core.investor_mandates",
        label="Investor mandates", module=MODULE,
        label_field="source", supports_custom_fields=False,
        fields=_spine({
            "subject_type": FieldSpec("subject_type", "text", column="subject_type",
                                      required=True),
            "subject_id": FieldSpec("subject_id", "uuid", column="subject_id",
                                    required=True),
            "source": FieldSpec("source", "select", column="source",
                                options=("response", "human", "ai")),
            "confidence": FieldSpec("confidence", "number", column="confidence"),
            "asset_classes": FieldSpec("asset_classes", "multiselect",
                                       column="asset_classes", options=ASSET_CLASSES),
            "stages": FieldSpec("stages", "multiselect", column="stages",
                                options=STAGES),
            "sectors": FieldSpec("sectors", "multiselect", column="sectors"),
            "geographies": FieldSpec("geographies", "multiselect", column="geographies"),
            "check_min": FieldSpec("check_min", "currency", column="check_min"),
            "check_max": FieldSpec("check_max", "currency", column="check_max"),
            "currency": FieldSpec("currency", "text", column="currency"),
            "hold_horizon_years": FieldSpec(
                "hold_horizon_years", "number", column="hold_horizon_years"),
            "liquidity_need": FieldSpec("liquidity_need", "select",
                                        column="liquidity_need", options=LIQUIDITY_NEEDS),
            "esg_constraints": FieldSpec("esg_constraints", "multiselect",
                                         column="esg_constraints"),
            # Writable, not system-only: provenance, not a security boundary
            # -- a human correcting a mandate may reasonably re-point it at a
            # different response. The derivation subscriber below sets it on
            # every auto-derive; nothing requires it to be the only writer.
            "derived_from_response_id": FieldSpec(
                "derived_from_response_id", "uuid", column="derived_from_response_id"),
            "reviewed_by": FieldSpec("reviewed_by", "uuid", column="reviewed_by"),
            "reviewed_at": FieldSpec("reviewed_at", "datetime", column="reviewed_at"),
        }),
    ))

    registry.register_validator(
        "commitment", _validate_commitment_closing,
        actions=("update",), module=MODULE,
    )
    registry.register_validator(
        "investment_account", _validate_investment_account_activation,
        actions=("create", "update"), module=MODULE,
    )
    registry.register_org_seed(_seed_investor_categories, module=MODULE)
    events.subscribe(
        "investor_portal.derive_mandate", _derive_mandate,
        entities=("questionnaire_response",), actions=(events.CREATED,),
    )
