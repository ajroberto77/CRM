# Data Model

## Identity, conflicts, attribution, and history

These four questions come up in any contact-tracking system and matter
specifically for comparing this CRM against a sibling system before a merge.
Answered here directly from source, not inferred — and where the CRM simply
doesn't have a mechanism, that's stated plainly rather than papered over.

### Duplicate people/companies

Identity is **never matched on name.** `server/core/identity.py`'s
`normalize_name()` docstring says why directly: "Matching people by name
alone merges strangers... never for matching on its own." The real de-dup key
is `core.contact_channels`, unique on `(kind, value_normalized)` — an email,
phone number, or messaging handle, each run through the one normalizer for
its kind (`identity.normalize_email`/`normalize_phone`/etc.) before matching.
Two different channels resolving to the same normalized value collapse to one
`person`; a channel with no match creates a new one.

A `person`/`organization` created this way carries `is_derived=True` and
`source="derived"` (or `"sync"`, `"import"`, `"ai"` depending on origin) —
distinguishing "the system inferred this contact exists" from "a human
entered this." The moment a human edits an `is_derived` record, an event
subscriber (`server/core/derivation.py`'s `_promote_on_human_edit`) flips it
to `is_derived=False` — a derived record graduates to a confirmed one on
first human touch, never before.

**There is no fuzzy/probabilistic duplicate-detection pass** (no "these two
records look like the same person, review and merge?" workflow) — dedup is
entirely a side effect of channel-identity matching at write time, not a
separate reconciliation process.

### Conflicting information

**No dedicated "sources disagree" mechanism exists.** If two channels or two
sync sources report different values for the same logical fact (two
different job titles for the same person, say), the generic repository has
no built-in resolution — whichever write happens last simply overwrites the
field, same as any ordinary CRUD update. This is a real gap worth surfacing
plainly rather than describing a discipline that isn't actually enforced.

The two structural features that come closest, both for different reasons:
- **`metric_fact`** is period-shaped, not point-shaped, specifically so
  competing values don't have to overwrite each other: "Acme's Q3 EBITDA" and
  "Acme's Q4 EBITDA" are two separate `metric_fact` rows
  (`period_start`/`period_end`), each carrying its own `source`
  (`human`/`sync`/`ai`) and `confidence`. This solves conflicting facts across
  *time*, not conflicting facts about the *same* time from different sources.
- **`custom` fields and `note`** are free-form and can informally hold
  "per X, the number is Y; per Z, it's W" — but this is a convention a user
  would have to adopt themselves; nothing in the schema or the UI prompts for
  it or enforces it.

### Attribution (who-said-what)

Two real, structural mechanisms, answering two different questions:
- **"Who changed this record"** — `core.events` (the transactional outbox
  described in `01-architecture.md` §6) records `actor_user_id`/`actor_kind`
  plus a full field-level diff (`before`/`after`) for every create/update/delete,
  system-originated writes clearly distinguished (`is_system`,
  `system_reason`) from human ones.
- **"Who told us this fact" / body-level provenance** — `interaction.body`
  is scoped by `owner_id` under safety rule 10 ("message bodies are
  owner-scoped, interaction metadata is org-wide") specifically so a mailbox
  owner's correspondence isn't readable org-wide just because a contact
  appears in two mailboxes. `interaction.from_channel`/`to_channels` plus
  `interaction_participants` rows record which contact-channels were actually
  on a given communication.

What's **not** structurally attributed: a `note` has an `owner_id` (who wrote
it) but no field distinguishing "I observed this directly" from "I heard this
from someone else" — that distinction, if wanted, would again be a
convention inside the note's free text, not a schema-enforced field.

### History (how a read on someone changed over time)

Three real mechanisms, none of them a dedicated "assessment history" field:
- **`core.events`** — every field-level change to every record, forever
  (subject to whatever retention policy an operator sets at the infra level;
  nothing in the schema itself expires an event row). This is the closest
  thing to a full audit trail of how a record's data changed.
- **`core.associations`' `valid_from`/`valid_to`** — a relationship is ended
  by setting `valid_to` (`end_association()`), never by deleting the row, so
  "was CFO until March" stays answerable rather than being destroyed the
  moment the relationship ends. `dissociate()` (hard delete) exists
  separately for a relationship that was simply wrong, not one that ended.
- **The interaction log itself** — immutable, dated communications
  (`core.interactions`) are the platform's own "derived view, not a system of
  record" bet (see `01-architecture.md`'s headline architectural decision):
  the intent is that a relationship's real history lives in what was actually
  said and when, not in a human's periodically-updated summary of it.

**There is no dedicated "my read on this person changed" narrative field** —
no sentiment/assessment-over-time tracking distinct from the ordinary `note`
entity. A user wanting that today would keep dated notes and rely on `note`'s
own `created_at`/`updated_at`, informally, the same way they'd keep a diary.

---

## Every registered entity

26 entities across core and the two installed modules. `module` shows who
registered it; `nav` shows where (if anywhere) it appears in the sidebar.

### Core (`server/core/registry.py`)

| Entity | Table | Purpose |
|---|---|---|
| `organization` | `core.organizations` | A company — deliberately undifferentiated by role (R6: "a role is not an entity type"). Carries `is_internal`, `is_derived`, `source`, `domicile_country`, and (added this session) `is_public`, plus the computed `role_summary`. |
| `person` | `core.persons` | A person, same undifferentiated-by-role treatment. Carries `tax_residence_country`/`citizenship_country`, `auto_accept` (trust flag), `is_derived`, and the computed `role_summary`. |
| `pipeline` | `core.pipelines` | Admin-configurable deal-stage list (`stages` jsonb). |
| `trusted_sender` | `core.trusted_senders` | Email/domain patterns that auto-accept scheduling proposals (see `server/core/trust.py`). |
| `deal` | `core.deals` | The core sales pipeline entity. Computed `rotting` flag (open + no activity past a configurable threshold) — the deliberately-simple "formula, not a job" implementation. |
| `task` | `core.tasks` | Generic, subject-polymorphic to-do. |
| `note` | `core.notes` | Generic, subject-polymorphic free text. |
| `document` | `core.documents` | Generic, subject-polymorphic file/e-signature envelope — core, not vertical, so `modules/investor_portal` can be its first real consumer without owning it. |
| `interaction` | `core.interactions` | The primitive (see `01-architecture.md`'s headline bet). `body` is owner-scoped (safety rule 10); everything else is org-wide. Computed `display_label`. |
| `contact_channel` | `core.contact_channels` | A person's email/phone/handle, unique on `(kind, value_normalized)` — the actual identity-resolution surface. |
| `metric_fact` | `core.metric_facts` | Period-shaped facts (subject-polymorphic) — see "Conflicting information" above. |
| `proposed_change` | `core.proposed_changes` | The one approval queue (`server/core/proposals.py`). Every field `writable=False` through the generic path — only `approve()`/`decline()`/`auto_approve()` may transition it. |
| `saved_view` | `core.saved_views` | A user-defined filter/sort/column/group-by spec, recompiled and re-permission-checked on every execution. |
| `custom_field` | `core.custom_fields` | Org-defined field definitions — admin-configurable schema extension without a migration. |

### `modules/funds`

| Entity | Table | Purpose |
|---|---|---|
| `fund` | `core.funds` | A fund vehicle. `entity_org_id` points at an `organization` — a fund *is* a legal entity, so another GP's fund is the same shape as your own. |
| `commitment` | `core.commitments` | Who committed what to which fund. `investor_org_id`/`investor_person_id`/`investment_account_id` — an LP is an org, a person, or (since Phase B) an account. |
| `investment_account` | `core.investment_accounts` | The actual vehicle that commits capital — a trust, LLC, IRA, SPV, personal account. |
| `gp_role` | `core.gp_roles` | Admin-configurable "what does this person do at the GP" reference list (Managing Partner, CFO, ...). |
| `security` | `core.securities` | Ticker/exchange/listing history for a public target/portfolio company (added this session, Phase 10) — kept off `organization` itself since it's lifecycle-shaped, not a flag. |

### `modules/investor_portal`

| Entity | Table | Purpose |
|---|---|---|
| `investor_category` | `core.investor_categories` | Legal/regulatory investor classification reference list, seeded per org. |
| `investment_pathway` | `core.investment_pathways` | The firm's CRM-facing program name. |
| `pathway_vehicle` | `core.pathway_vehicles` | Join row (pathway × fund) — the one `investor_portal` table with an FK into `modules/funds`. |
| `investor_profile` | `core.investor_profiles` | Status/accreditation/relationship record attached to an org or person. |
| `questionnaire` | `core.questionnaires` | Versioned question set. |
| `questionnaire_response` | `core.questionnaire_responses` | An investor's answers to one questionnaire version. |
| `investor_mandate` | `core.investor_mandates` | Investment criteria, partly auto-derived from questionnaire answers (fail-safe on skipped questions — see `07-modules.md`). |

## Every association role

19 roles. `from_types`/`to_types` shown as `from → to`; symmetric/hierarchical
roles noted.

### Core (`server/core/registry.py`)

| Role | from → to | Notes |
|---|---|---|
| `works_at` | person → organization | inverse: "employs" |
| `board_member_of` | person → organization | |
| `advisor_to` | person → organization, person | |
| `introduced_by` | person, organization → person | |
| `vendor_to` | organization → organization | inverse: "vendors" |
| `owned_by` | organization → organization, person | **hierarchical** (walked by `hierarchy.py`'s cycle-checked recursive CTE) — the ultimate-parent-company chain. |

### `modules/funds`

| Role | from → to | Notes |
|---|---|---|
| `lp_in` | organization, person, investment_account → fund | inverse: "investors" |
| `gp_of` | organization, person → fund | inverse: "general partners" |
| `portfolio_of` | organization → fund | inverse: "portfolio" — an actual investment. |
| `evaluating` | fund → organization | inverse: "being evaluated by" — a prospective target, added this session (Phase 10), deliberately a separate role from `portfolio_of` rather than a status flag. |
| `co_investor_in` | organization → organization | **symmetric** (canonicalized on write). |
| `lender_to` | organization → organization | |
| `acquirer_of` | organization → organization | |
| `rolls_up_to` | organization, investment_account → organization | **hierarchical** — the investment-relationship rollup, deliberately *not* legal-entity-based (unlike `owned_by`). |
| `principal_of` | person → organization | Carries an optional `attributes.gp_role_key` soft reference. |
| `authorized_signer_for` | person → organization, investment_account | |
| `account_holder_of` | person → investment_account | |
| `trustee_of` | person → investment_account | |
| `beneficiary_of` | person → investment_account | |

No association roles are added by `modules/investor_portal` — investor
classification is a property of the existing org/person record
(`investor_profile`/`investor_mandate`, keyed by `subject_type`/`subject_id`),
not a new relationship type.

## Database conventions (for comparison against a schema built independently)

- Every table carries `org_id`; RLS enforces the tenant boundary (never an
  application-level filter).
- Every record table also carries the "spine": `id`, `owner_id`,
  `created_at`, `updated_at`, `custom` (jsonb, for org-defined fields).
- No table or column name is ever guessed — `server/db/schema.py` (or the
  live schema) is the only authoritative source; design docs describe intent
  and can be stale (this audit found two concrete examples of that in
  `07-modules.md`).
- Four logical Postgres schemas: `core` (everything above), `sync` (delta
  cursors, sync links), `jobs` (the M7 work queue), `ai` (embeddings).
