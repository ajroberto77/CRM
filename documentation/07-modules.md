# Modules: `investor_portal` and the E-Signature Dispatcher

## 1. `modules/investor_portal/manifest.py` — entities, roles, tables, seeds

`MODULE = "investor_portal"`.

**Entities registered via `register(EntitySpec(...))`:**

| Entity name | Table | Notes |
|---|---|---|
| `investor_category` | `core.investor_categories` | admin_only, no custom fields. Reference data for legal/regulatory investor classification. |
| `investment_pathway` | `core.investment_pathways` | admin_only. The firm's CRM-facing program name. |
| `pathway_vehicle` | `core.pathway_vehicles` | admin_only, `nav="none"` — pure join row (pathway × fund), the one table that references `core.funds.id`. |
| `investor_profile` | `core.investor_profiles` | `label_field="display_label"` (computed). `nav_group="Investors"`. Status/accreditation/relationship record attached to an org/person. |
| `questionnaire` | `core.questionnaires` | admin_only. Versioned question set. |
| `questionnaire_response` | `core.questionnaire_responses` | `nav="none"`. |
| `investor_mandate` | `core.investor_mandates` | `nav="none"`, unique per subject. |

**`TABLES`** — six tables, all module-owned (per R6). `pathway_vehicles` is the
only table with an FK into `core.funds` — a one-way dependency; `modules/funds`
gains no awareness of `investor_portal`.

**Association roles: none added.** The module's own docstring is explicit:
"No new `AssociationRole` is needed here; classification is a property of the
existing organization/person record, not a new relationship." Investor status
stays attached via `investor_profile`/`investor_mandate` rows keyed by
`subject_type`/`subject_id`, not via `associations`.

**Org-seed**: `_seed_investor_categories(org_id)` inserts 6 rows —
`accredited_individual` and `accredited_entity` (`is_enabled=True`),
`qualified_purchaser`, `qualified_client`, `institutional`, `non_accredited`
(`is_enabled=False`, all four requiring verification except `non_accredited`).
Runs through ordinary `repository.create()` with a system principal.

**Event subscription**: `"investor_portal.derive_mandate"` on
`questionnaire_response` CREATE.

## 2. Design-doc intent vs. what's actually implemented

`docs/INVESTOR-PORTAL.md` describes a much larger surface than what
`manifest.py` implements, and its own phased plan is only partly accurate
against the current code:

| Phase | Doc says it delivers | Actually implemented in code |
|---|---|---|
| **M9a** | investor_profiles, investor_categories, investment_pathways/pathway_vehicles, questionnaires + versioned responses, mandates, review UI | **Built** — all 7 entities above exist, tested in `tests/test_investor_portal_m9a.py`. Doc's own status column says "Not started" — **stale relative to the code**. |
| **M9b** | Matching in both directions, `offerings`, human-granted `offering_grants` | **Not built at all.** No `offerings` or `offering_grants` table/entity exists anywhere. Doc status "Not started" is accurate. |
| **M9c** | External identity class, self-serve questionnaire through a gated portal, public marketing site | **Not built.** No `auth.identities` change, no external-identity router, no public-site code. Doc status accurate. |
| **M9d** | `core.documents`/`core.document_signers`, `esign` dispatcher (4 vendors), commitment-closing gate | **Built** — confirmed below (§3–4). Doc marks this "Built — 34 tests," which matches. `document_templates` (merge-field generation) is explicitly still deferred per the doc and confirmed not built. |

**So "the LP portal" as a whole is far from fully implemented**: the actual
portal (external-facing UI, gated login, self-serve questionnaire delivery,
matching/offerings) is 100% design intent, not code. What exists is the
internal classification scaffolding (M9a) plus the compliance/document-gating
machinery (M9d) — both of which the design doc itself is candid about being
the only phases done.

There is also a second validator (the account-activation gate, §2 below) that
is **not described anywhere in either design doc at all** — documented only in
the module's own docstring. This is documentation drift in the *other*
direction: code that has outrun the design docs, and worth a dedicated
subsection in `docs/INVESTOR-PORTAL.md`.

## 3. Validators registered via `registry.register_validator()`

Two validators, both authored against the generic validator mechanism
(synchronous, runs inside the write's own transaction after the row is
written but before commit, raise-to-abort):

**a) Commitment-closing gate** — fires only on `update` where
`after.status == "closed"` and `before.status != "closed"`. Checks
`core.documents` for a `subject_type="commitment"`, `kind="subscription_agreement"`,
`status="executed"` row; raises `SubscriptionAgreementMissing` if none found.
This is precisely "a commitment cannot close without an executed subscription
agreement." Tested exhaustively in `tests/test_validators.py`.

**b) Investment-account activation gate** — `investment_account` is a
`modules/funds` entity, not owned by `investor_portal` (same one-way
cross-module dependency pattern). Fires on transition into `status='active'`,
on **both** create and update. Gated behind a per-org setting
(`core.settings`, section `"compliance"`, key `enforce_account_activation_gate`),
**defaulting to `True`** — compliance gates default ON, unlike destructive
actions which default OFF (contrasted explicitly against safety rule 9 in the
docstring). Required document kinds are derived from the held person's
`tax_residence_country` or the account's `domicile_country`: always
`AML_KYC_KIND`; plus `W9_KIND` if US, `W8BENE_KIND` if non-US entity,
`W8BEN_KIND` if non-US individual. **If neither country is on file, the
function returns `None` and the caller blocks rather than only requiring
AML/KYC** — the module's explicit instance of safety rule 8 ("a null category
never matches an auto-accept rule"). Tested in
`tests/test_account_activation_gate.py`.

Both gates share one helper for "does an executed, unexpired document of kind
X exist for subject Y" (R1 discipline).

## 4. `server/providers/esign.py` — the sixth dispatcher (R3)

`PROVIDERS = ("docusign", "dropboxsign", "pandadoc", "adobesign")`.
`STATUSES = ("draft", "sent_for_signature", "partially_signed", "executed",
"void", "expired")` — matching `core.documents.status`'s CHECK constraint.

Three dispatch functions, each an if/elif chain importing the vendor-specific
module by name: `create_and_send()`, `check_status()` (also validates the
returned status against the closed vocabulary), `download_executed()`.
`_request_with_retry()` wraps the shared HTTP-retry transport, translating a
bare 409 into `NotReadyError`. `EsignDisabled` is raised loudly, never
silently skipped, when credentials are missing.

**This is not a stub — all four adapters are fully implemented**, each
carrying a "never live-tested" caveat but implementing real HTTP calls
against the vendor's documented REST API:
- **DocuSign**: OAuth2 JWT Bearer Grant (RSA/SHA256 signing), shard-discovered
  base URI via `GET /oauth/userinfo` (cached), one-call create-and-send.
- **Dropbox Sign**: HTTP Basic auth (API key as username), hand-rolled stdlib
  multipart encoder (the only adapter needing raw file upload), one-call send.
- **PandaDoc**: two calls with a wait — create, poll until draft, then send.
  `download_executed()` deliberately uses `/download-protected`.
- **Adobe Sign**: static bearer token, base URL shard-discovered via `GET
  /baseUris` (cached), two calls (transient document, then agreement).
  Docstring flags its declined/cancelled status mapping as the softest-confirmed
  part of the integration.

Test coverage (`tests/test_esign.py`) exercises dispatch, retry translation,
DocuSign JWT construction (signature-verified against the key's own public
half), and document/signer plumbing — all HTTP mocked, no real vendor call is
ever made in tests, consistent with the "never live-tested" caveat in the
code.

**Verdict: fully implemented, not stubbed** — real, working adapters against
documented APIs, just unverified against live vendor sandboxes.

## 5. Tie-in to `core.documents` for subscription agreements

`core.documents` is a **core** entity (`document`), not owned by
`investor_portal` — a deliberate design decision: "e-signature is a natural
extension of [the generic documents] capability... it belongs in core, with
`modules/investor_portal` as its first real consumer rather than its owner."
Fields include `subject_type`/`subject_id` (polymorphic), `kind`, `filename`,
`storage_key`, `provider`/`provider_envelope_id` (free text — so a new esign
provider never needs a schema change), and `status` (same closed vocabulary as
`esign.STATUSES`).

The module defines the actual kind-string vocabulary once
(`SUBSCRIPTION_AGREEMENT_KIND`, `W9_KIND`, `W8BEN_KIND`, `W8BENE_KIND`,
`AML_KYC_KIND`, `PROOF_OF_ADDRESS_KIND`, `ACCREDITATION_EVIDENCE_KIND`). The
tie-in is entirely through `subject_type`/`subject_id`/`kind` filtering —
`core.documents` has zero awareness of `commitment` or `investment_account`.
`core.document_signers` (per-signer rows) is deliberately **not** a registered
entity — accessed only through helper functions in `server/core/documents.py`.
`document_templates` (merge-field generation) remains the one deferred piece
of the original scope — confirmed not built.

## 6. Mandate/questionnaire auto-accept and fail-safe logic

`_derive_mandate()`, subscribed to `questionnaire_response` creation. For each
question with a `maps_to` in the questionnaire's schema, if the investor
answered it, that answer is copied onto the mandate field. **A question the
investor skipped has no key in `answers`, so the mandate field is simply
never set** — left `null` rather than defaulted to a "matches everything"
sentinel. This is the concrete instance of safety rule 8, tested directly
(`test_a_skipped_question_leaves_the_mandate_field_null`).

The same fail-safe pattern reappears in the account-activation gate's
required-document-kinds function (§3b): when tax country can't be determined,
it returns `None` rather than a partial list, and the caller treats `None` as
"block."

**No auto-accept/auto-grant logic exists for offerings or access** — by
design, per the design doc: "A match is a ranking and a suggestion, never an
automatic grant." That mechanism (`offering_grants`) is unbuilt.

## 7. Summary: design intent vs. tested reality

| Claim | Design-doc status | Actual code / test evidence |
|---|---|---|
| Investor classification (categories, pathways, profiles, questionnaires, mandates) | Doc lists as "Not started" | **Implemented and tested** — doc is stale here |
| `investor_categories` seeded 2-enabled/4-disabled per org | Described | **Matches exactly** |
| Matching, `offerings`, `offering_grants` | "Not started" | **Not built** — accurate |
| External identity, gated portal, self-serve questionnaire, public site | "Not started" | **Not built** — accurate |
| `core.documents`/`document_signers`, esign dispatcher (4 vendors), commitment-closing gate | "Built — 34 tests" | **Confirmed built and tested** |
| `document_templates` | Explicitly deferred | **Confirmed not built** |
| Investment-account AML/KYC/tax-form activation gate ("Phase D") | **Not mentioned in either design doc** | **Implemented and tested** — undocumented-but-real code |
| Commitment backfill to `investment_account` (a `modules/funds` artifact feeding the activation gate) | Referenced only obliquely | **Implemented and tested** — `modules/funds/backfill.py`, `tests/test_commitment_backfill.py` |

**Overall assessment**: `docs/INVESTOR-PORTAL.md` is accurate for M9b/M9c
(both correctly marked not-started) and largely accurate for M9d, but stale
for M9a (marked not-started, actually built) and silent on the
account-activation gate (real, tested, undocumented). Closing that last gap —
giving the activation gate its own section in the design doc — is the single
highest-value doc fix identified in this audit.
