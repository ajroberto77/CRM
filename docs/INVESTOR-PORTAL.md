# Investor website and investor classification

A second surface on the same platform: a public site plus a gated investor
portal, and a questionnaire that classifies investors by what they actually want
to invest in. Read `docs/DESIGN.md` and `docs/VERTICAL-ASSET-MANAGEMENT.md`
first.

Reference model: **Florida Funders** — a hybrid of an institutional VC fund and
an accredited investor network. Three pathways (an institutional fund, a
deal-by-deal tier at a $50K minimum, and a network tier from $5K per deal with
no upfront commitment), self-accreditation at registration, offerings under
Reg D **506(b)**, and a gated portal at a separate hostname from the marketing
site. Screenshots from behind their login are still to come; the sections marked
**[needs screenshots]** are where they will change the design rather than
confirm it.

**Eight decisions confirmed by the user:**

1. **Accredited investors only, today.** No non-accredited tier, no Reg CF/A+
   crowdfunding path active. But the investor-type model must **not** hard-code
   this — see "Investor categories are extensible" below. What is inactive today
   is a disabled row, not an assumption baked into a schema or an enum.
2. **Self-certifying.** This settles 506(b) vs 506(c) on its own: 506(c) legally
   *requires* verified accreditation, and self-certification does not satisfy
   it. So the offering exemption is **506(b)**, and `accreditation_method`
   defaults to `self_certified` with no verification vendor to integrate in v1.
   The field stays multi-valued (see the schema below) because a future
   investor category — a qualified purchaser for a 3(c)(7) vehicle, say — may
   need real verification even while the general population self-certifies.
3. **The public site will never show an offering. Full stop — not a 506(b)
   consequence that would loosen under 506(c), a permanent product decision.**
   This is simpler to build than a conditional rule, and it is enforced
   structurally, not just by a permission check — see below.
4. **The questionnaire is self-serve.** An investor fills it in themselves,
   through the portal — which puts the external identity class (M9c) on the
   critical path for the questionnaire's *delivery*, even though its schema,
   versioning and mandate derivation (M9a) do not depend on M9c at all and can
   be built and tested first, with the team previewing/answering internally.
5. **Investment pathways are separate legal vehicles today**, with the model
   kept open to a shared-vehicle pathway later — see "Pathways" below. This was
   the least obvious of the five and gets its own section because it is a real
   schema decision, not just a policy toggle.
6. **E-signature and subscription documents are wanted for v1.** This is the
   platform's **sixth provider axis** — `CLAUDE.md`'s R3 is updated accordingly,
   deliberately, rather than letting a dispatcher appear without the same
   sign-off every other axis got. See "Subscription documents and e-signature"
   below.
7. **No plans for a second investor category right now, but "who knows."**
   Nothing to build differently — this simply confirms the extensible-registry
   design in point 1 was the right call rather than premature generality.
8. **Pathway names and minimums are deferred.** Not blocking: `investment_pathways`
   ships with the extensible shape in M9a regardless, and gets seeded with real
   rows whenever the names are decided.

## The wall between the public site and the portal

Offerings live only behind the gate. This is not framed as "506(b) requires it
today" — it is a standing rule of the product, independent of exemption type,
and it must survive even if the firm later qualifies for 506(c) solicitation.

| Surface | May contain | Must not contain |
|---|---|---|
| Public site | Who the firm is, thesis, team, past portfolio, "register interest" | **Any offering, ever** — no terms, no allocation, no deal-specific material, no exceptions |
| Gated portal | Live offerings, terms, documents, allocations | — visible only to investors who are qualified **and** granted |

Because "never" is easy to state and easy to violate by accident (a shared
component, a careless join, a debug endpoint), it gets the same structural
treatment as R6's "core must never mention a fund":

- **The public site is a separate codebase surface with no import path to
  `offerings`, `offering_grants`, or anything in `modules/investor_portal`
  that touches them.** Not a permission check the public router happens to
  pass — the code to query an offering is simply unreachable from that
  process.
- A tokenized test (the same technique as `test_core_never_mentions_a_fund`)
  asserts the public-site package contains no reference to `offerings`
  identifiers. A permission bug can be exploited; an import that does not
  exist cannot be.
- The portal itself withholds an offering at the **query layer** (the
  visibility predicate), never merely by hiding a link in the UI.

Two consequences from the interaction-log design that still apply and are worth
building in rather than retrofitting:

1. **The relationship must be evidenced and dated**, even though solicitation
   restrictions are now a product choice rather than only a legal floor. The
   questionnaire, and the interactions preceding it, remain part of the record
   of when a relationship began.
2. **Qualification state gates rendering, not just navigation** — restated
   above as a structural rule rather than a policy.

*Not legal advice.* The design should still be reviewed by the firm's
securities counsel before launch. "Self-certifying, 506(b), never public" is
the assumption this document builds on; if any of the three changes, this
section is what to revisit first.

## Investors are still not an entity type

The rule from `docs/VERTICAL-ASSET-MANAGEMENT.md` holds. An investor is an
ordinary `organization` or `person` with `lp_in` associations. What is new is
**what they want** — a mandate — and **whether they may see it** — a
qualification. Both attach to the existing record rather than replacing it.

This matters here specifically because the same family office is routinely a
prospect on one fund, a committed LP on another, and a co-investor on a deal.
One record, three roles, one history.

## New module: `modules/investor_portal`

Core stays domain-neutral (R6). The portal is a module, like `funds`.

```
investor_categories(id, org_id, key, label, requires_verification,
                    is_enabled, sort_order)

investment_pathways(id, org_id, key, label, description, default_min_check,
                    default_currency, is_enabled, sort_order, custom)

pathway_vehicles(id, org_id, pathway_id, fund_id, added_at)

investor_profiles(id, org_id, subject_type, subject_id,
                  category_id, pathway_id, status, accreditation_method,
                  accredited_at, accreditation_expires_at, verified_by,
                  relationship_since, min_check, max_check, custom)

questionnaires(id, org_id, name, version, schema jsonb, published_at, retired_at)

questionnaire_responses(id, org_id, questionnaire_id, questionnaire_version,
                        subject_type, subject_id, answers jsonb,
                        submitted_at, submitted_by)

investor_mandates(id, org_id, subject_type, subject_id, source, confidence,
                  asset_classes text[], stages text[], sectors text[],
                  geographies text[], check_min, check_max, currency,
                  hold_horizon_years, liquidity_need, esg_constraints text[],
                  derived_from_response_id, reviewed_by, reviewed_at)

offerings(id, org_id, fund_id, deal_id, name, kind, status,
          minimum, currency, opens_at, closes_at, visibility_rule jsonb)

offering_grants(id, org_id, offering_id, subject_type, subject_id,
                granted_by, granted_at, revoked_at, reason)
```

`status` on `investor_profiles`: `prospect | questionnaire_sent |
questionnaire_complete | self_accredited | verified | active | lapsed |
declined`.

Everything lives in `modules/investor_portal`, including `pathway_vehicles`,
which is the one table that references `core.funds`. The dependency runs one
way only: `investor_portal` knows about `funds`, exactly as `offerings.fund_id`
already does — `modules/funds` gains no column, no table, and no awareness that
`investor_portal` exists. That already-built, already-tested module needs no
migration for any of this.

### Investor categories are extensible, not a code enum

Three distinct dimensions, easy to conflate and kept apart on purpose:

| Dimension | Answers | Where it lives |
|---|---|---|
| **Category** | What kind of investor, legally/regulatorily — accredited individual, accredited entity, qualified purchaser, qualified client, institutional, non-accredited | `investor_categories`, admin-configurable |
| **Pathway** | Which of the firm's programs they invest through | `investment_pathways`, admin-configurable — see below |
| **Mandate** | What they want to invest in — the questionnaire | `investor_mandates` |

`investor_categories` follows the same pattern as `core.custom_fields`: a
reference table an admin can extend, not a `CHECK` constraint baked into the
schema. Seeded at install with `accredited_individual` and `accredited_entity`
**enabled**, and `qualified_purchaser`, `qualified_client`, `institutional`,
`non_accredited` present but `is_enabled = false`. Turning one on later — to
run a 3(c)(7) vehicle that needs qualified purchasers, say, or to add a Reg CF
tier — is a data change and a UI toggle, never a migration or a code release.

`requires_verification` is per-category, which is what lets the platform be
self-certifying **today** without hard-coding that as the only path: a category
enabled later can require `accreditation_method = 'verified'` while
`accredited_individual` keeps self-certifying, with no schema change either
time.

The onboarding flow and the questionnaire only ever present **enabled**
categories, so nothing changes in what an investor sees until the firm
deliberately turns a category on.

### Pathways: separate legal vehicles today, without foreclosing a shared one

Confirmed: pathways today are **separate legal vehicles** — a distinct fund or
SPV per program — but the model must not hard-code that as the only shape a
future pathway can take.

`investment_pathways` is the CRM-facing program (the thing an investor and the
team talk about — "Main Fund", "Portfolio Select", whatever names the firm
actually uses). `pathway_vehicles` is a many-to-many join to the *real* legal
vehicles, `core.funds`, that implement it. Nothing about "dedicated vehicle" vs
"shared vehicle" is a stored mode or a branch in code — it is purely a
consequence of how many `funds` rows get linked to a pathway:

- **Dedicated vehicle (today's model):** each pathway links to its own fund, or
  gains a fresh one per vintage or per deal — a new SPV under "Network" is just
  another row in `pathway_vehicles` for that pathway.
- **Shared vehicle (available, not built for):** a future pathway could link to
  the *same* `fund_id` another pathway already uses — two access tiers into one
  pool of capital — with zero schema change, because the join table already
  permits it.

This is the same reasoning as `investor_categories`: one extensible mechanism,
not two mechanisms picked between at design time (R1). A commitment points at a
real `fund_id` either way, so `core.commitments` and everything built on it in
M1 is unaffected regardless of which shape a given pathway takes.

`investor_profiles.pathway_id` records which pathway an investor primarily
engages through, for onboarding and portal display; the authoritative record of
what they actually hold is still the `commitments` rows themselves, each
pointing at a real vehicle.

### Why questionnaires are versioned

`questionnaire_responses` stores the **version answered**, and the schema is
kept rather than mutated. Change a question and old responses stop meaning what
they said — a classification derived from "would you consider debt?" is not
comparable to one derived from "rank these by preference". Versioning is
cheap now and impossible to reconstruct later.

### Why the mandate is separate from the response

The response is what the investor said. The mandate is the structured,
queryable interpretation used for matching. Keeping them apart means:

- A mandate can be **corrected by a human** without falsifying the response.
- A mandate can be **derived by an LLM** from a conversation or a prior
  relationship rather than a form, carrying `source` and `confidence` — and, per
  the platform's precedence rule (`human > sync > ai`), an AI-derived mandate
  over a human-set field goes to `proposed_changes` rather than overwriting.
- Re-running classification after a questionnaire change is a backfill, not a
  data-loss event.

## The questionnaire

Asset classes as a closed vocabulary — free text drifts and silently stops
matching, the same failure Cal hit with event categories:

`venture` · `growth_equity` · `private_equity` · `public_equity` ·
`activism` · `private_credit` · `venture_debt` · `real_assets` ·
`infrastructure` · `secondaries` · `fund_of_funds` · `crypto` · `other`

Alongside asset class, the questions that actually drive matching:

| Dimension | Why it earns a column |
|---|---|
| Stage | seed / A / B / growth / late — the single strongest filter on VC deal fit |
| Check size (min/max) | separates a $5K network member from a $50K select investor from an LP |
| Sector | matched against the deal's sector, weighted not absolute |
| Geography | the reference firm is explicitly regionally focused |
| Hold horizon / liquidity | distinguishes venture appetite from credit appetite |
| Constraints | exclusions the firm must honor, and must be able to prove it honored |

Answers are stored as given; the mandate is derived. A question the investor
skips leaves the mandate dimension **null**, and a null never matches an
auto-inclusion rule — unclassifiable input fails safe, exactly as the approval
queue does elsewhere.

## Matching, and what it is allowed to do

Matching runs both directions off the same predicate:

- *Which investors fit this offering?* — for the raise.
- *Which offerings should this investor see?* — for the portal.

A match is a **ranking and a suggestion**, never an automatic grant — matching
does not, on its own, decide who sees an offering. The decision is a human
judgment the firm makes, recorded in `offering_grants` with who granted it and
why. That is the same `proposed_changes` discipline already in the platform,
and here it is also the audit trail. It holds regardless of exemption type; it
is not conditioned on 506(b) specifically.

## External identity — the decision, now made

`docs/DESIGN.md` deferred the LP portal but fixed its shape. This is that
decision landing:

> **An investor is a second identity class, not a permission level.**

- `auth.identities` gains `kind ∈ internal | external`. It already separates
  identity from tenant, so this is an additive change to a table that exists.
- External access is **grant-based**, never role-based. An external session
  resolves to the records granted to that subject — their own profile, their own
  commitments, their granted offerings — and to nothing else.
- External identities do **not** get a `Principal` with object permissions.
  Mixing external parties into `core/permissions.py` is how a "share with the
  investor" feature becomes an org-wide leak; the internal model must not learn
  about them at all.
- The portal API is a **separate router with its own dependency**, so no
  internal route can be reached with an external session by forgetting a check.
- Rate limiting and enumeration protection matter here in a way they do not
  internally: this surface faces the public.

## Subscription documents and e-signature

Confirmed as wanted for v1, not deferred to a later phase.

### Documents are core, not vertical

`docs/VERTICAL-ASSET-MANAGEMENT.md` already scoped a generic `documents` table
for M2 (subject-polymorphic, `valid_until` gating an action). E-signature is a
natural extension of that same generic capability — any CRM wants "generate and
sign a document tied to a record," not just this vertical — so it belongs in
**core**, with `modules/investor_portal` as its first real consumer rather than
its owner. This is the same split as `funds`/`core.organizations`: the generic
primitive lives centrally, the vertical meaning attaches through subject type.

```
core.documents(id, org_id, subject_type, subject_id, kind, filename,
               storage_key, mime, bytes, uploaded_by,
               status, valid_from, valid_until, custom)
-- status: draft | sent_for_signature | partially_signed | executed | void | expired

core.document_templates(id, org_id, key, name, kind, provider_template_id,
                        merge_schema jsonb, is_enabled, created_at)

core.document_signers(id, org_id, document_id, subject_type, subject_id,
                      role, order_index, status, signed_at, provider_signer_id)
-- role is free text ('investor', 'gp', 'witness'); status: pending|sent|viewed|signed|declined
```

`core.documents.subject_type`/`subject_id` is exactly how `tasks` and `notes`
already attach to arbitrary records, so a subscription agreement pointing at a
`commitment` (a `modules/funds` entity) needs no FK from `documents` to `funds`
and no awareness in either direction — the same one-way-dependency discipline
already used for `pathway_vehicles` → `core.funds`.

### The sixth provider axis

E-signature is dispatched exactly like the other five (R2/R3):

```
server/providers/esign.py          -- create_envelope(), send(), status(), download_executed()
server/providers/<provider>_esign.py   -- the first adapter
```

`CLAUDE.md`'s R3 is updated to six axes as part of this decision, not as a
side effect of writing the adapter — a dispatcher appearing without that
sign-off is exactly what R3's own text now warns against.

**Which vendor is still open** (DocuSign, Dropbox Sign, PandaDoc, Adobe Sign
all fit the shape) — see open questions. The dispatch contract does not change
based on the answer; only which `<provider>_esign.py` file gets written first.

### The workflow does not require the portal to exist

A useful decoupling, worth stating because it changes sequencing: most
e-signature providers deliver via their **own hosted signing page**, reached by
an emailed link — the investor does not need to be logged into this platform's
portal to sign. So the subscription-document workflow (generate from a
template + commitment data, send for signature, track status, mark the
commitment's paperwork complete) can ship as soon as `core.documents` and the
`esign` dispatcher exist, independent of M9c's external identity class and
gated portal. The portal, once it exists, upgrades the experience — a
"documents" tab with live status instead of an email thread — but is not on the
critical path for e-signature to work at all.

### The gate — built

**A commitment cannot reach `status = 'closed'` without an executed
subscription document.** M1's event bus could not enforce this — its
subscribers run *after* commit and cannot roll a write back, by design (see
`server/core/events.py`'s docstring). The gate needs to run *inside* the
write, before commit, and be able to abort it.

`server/core/registry.py` now has that mechanism:

```python
registry.register_validator(entity_name, fn, *, actions=(...), order=100)
```

`fn` receives a `ValidationContext` (`principal`, `entity`, `action`,
`record_id`, `before`, `after` — `after` is the real, fully-written row, not a
hand-rolled guess) and raises to abort. `server/core/repository.py` calls
every registered validator for an entity/action **after the row is written,
still inside the transaction** — so raising rolls back cleanly, and `after`
reflects defaults and normalization exactly as they would be stored. Core
calls whatever validators are registered without knowing what they check, so
this gate lives entirely in `modules/investor_portal/manifest.py` — neither
`repository.py` nor `modules/funds` mentions a commitment or a document.

`core.documents`, `core.document_signers` and `server/providers/esign.py`
(the dispatcher for all four vendors — DocuSign, Dropbox Sign, PandaDoc, Adobe
Sign, none chosen as default) are built alongside it, ahead of the rest of
M9d, specifically so this gate has something real to check: it looks for a
`document` row with `subject_type='commitment'`, `subject_id=<the
commitment>`, `kind='subscription_agreement'`, `status='executed'`. 34 new
tests cover the generic mechanism (order, action-scoping, rollback-on-raise,
before/after correctness), the gate itself (missing document, wrong kind,
wrong subject, the executed case, and that it only fires on the transition
*into* closed), the dispatcher (unknown provider, unconfigured provider,
retry-on-429, no-retry-on-409), and DocuSign's JWT construction — the
signature is verified against the key's own public half, not just asserted to
exist.

Each adapter is written from the vendor's current REST documentation and
carries the same "never live-tested" caveat Cal's own `microsoft_calendar.py`
carries for its unverified timezone handling — confirm each against a real
sandbox account before enabling it unattended. Per-vendor notes worth knowing
before that: DocuSign needs an RSA keypair and JWT consent per impersonated
user (a new, narrow `cryptography` dependency, chosen over hand-rolling
RS256 signing in stdlib); Dropbox Sign has no separate sandbox host, only a
`test_mode` flag that still fires real webhooks; PandaDoc needs two calls with
a wait for `draft` status in between, and its completed-document download
endpoint 401s on a sandbox key; Adobe Sign's base URL is shard-discovered per
account and its exact "declined" status representation is the one thing the
research could not fully confirm from documentation alone.

## What the public site is

Small, and mostly static: who the firm is, thesis, team, portfolio (past
investments are not an offering), news, and a **register interest** form that
creates a `prospect` and sends the questionnaire. No offering data, no terms, no
allocations — enforced structurally (see "The wall" above), not by a checklist.

It shares `tokens.css` with the app — same visual family, and the design system
already exists.

**[needs screenshots]** Whether the portal shows a deal room per offering, how
documents and e-signature are handled, whether allocations are self-service, and
what the investor's own dashboard reports. Those will change this design.

## Where this sits in the plan

A new milestone, after the internal product is usable. It depends on
`modules/funds` (M1, done), the approval queue (M5), and the external-identity
work is best done once the internal permission model has stopped moving.

| Phase | Delivers | Status |
|---|---|---|
| **M9a** | `investor_profiles`, `investor_categories`, `investment_pathways` + `pathway_vehicles`, questionnaires with versioned responses, mandates, and the internal UI to review and correct a classification | Not started |
| **M9b** | Matching in both directions, plus `offerings` and human-granted `offering_grants` | Not started |
| **M9c** | External identity class, the self-serve questionnaire delivered through the gated portal, and the public marketing site | Not started |
| **M9d** | `core.documents`/`document_signers`, the `esign` provider dispatch (all four vendors), and the commitment-closing gate | **Built** — 34 tests, see "The gate — built" above. `document_templates` (merge-field generation from a template) is the one piece of the original M9d scope still deferred; nothing found in `docs/VERTICAL-ASSET-MANAGEMENT.md`'s design needed it to close this gap |

Sequencing note: **M9a is worth doing early regardless**, because classifying
existing investors is useful to the internal CRM on its own, and the
questionnaire data is what M9b's matching needs to be worth building.
`investor_categories` and `investment_pathways` are both seeded as part of
M9a, so the extensibility exists from the first migration even though only a
few of each start enabled — and the questionnaire's schema, versioning and
mandate derivation can be built and tested in M9a even though self-serve
*delivery* of it waits on M9c's external identity class.

**M9d does not depend on M9c.** Because signing happens on the provider's own
hosted page reached by an emailed link (see "The workflow does not require the
portal to exist" above), subscription documents can go out and get signed as
soon as `core.documents` and the `esign` dispatcher land in M2 — the gated
portal is a UX upgrade for that flow, not a prerequisite.

## Confirmed

See the eight numbered decisions at the top of this document: accredited-only
as an extensible category; self-certifying / 506(b); the public site never
shows an offering; self-serve questionnaire; pathways as separate legal
vehicles kept open to a shared-vehicle model; e-signature and subscription
documents wanted for v1, landing as the platform's sixth provider axis; no
near-term plan for a second investor category; and pathway names/minimums
deferred without blocking anything.

## Open questions for John

1. **Which e-signature provider** — DocuSign, Dropbox Sign, PandaDoc, Adobe
   Sign? The dispatch shape (`server/providers/esign.py`) does not change based
   on the answer; only which `<provider>_esign.py` adapter gets written first,
   and what API credentials to provision.
2. **What are the pathways actually called, and what's each one's minimum
   check?** Needed to seed `investment_pathways` with real rows rather than
   placeholders modeled on Florida Funders' naming. Not blocking.
