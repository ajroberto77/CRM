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

**Three decisions confirmed by the user:**

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

investor_profiles(id, org_id, subject_type, subject_id,
                  category_id, status, tier, accreditation_method,
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
declined`. `tier` maps to the firm's pathways (fund / select / network), which
is how one platform serves all three without branching per tier in code.

### Investor categories are extensible, not a code enum

Three distinct dimensions, easy to conflate and kept apart on purpose:

| Dimension | Answers | Where it lives |
|---|---|---|
| **Category** | What kind of investor, legally/regulatorily — accredited individual, accredited entity, qualified purchaser, qualified client, institutional, non-accredited | `investor_categories`, admin-configurable |
| **Tier** | Which of the firm's pathways they invest through — fund / select / network | `investor_profiles.tier` |
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
`modules/funds` (M1, done), documents (M2), the approval queue (M5), and the
external-identity work is best done once the internal permission model has
stopped moving.

| Phase | Delivers |
|---|---|
| **M9a** | `investor_profiles`, questionnaires with versioned responses, mandates, and the internal UI to review and correct a classification |
| **M9b** | Matching in both directions, plus `offerings` and human-granted `offering_grants` |
| **M9c** | External identity class, the gated portal, and the public marketing site |

Sequencing note: **M9a is worth doing early regardless**, because classifying
existing investors is useful to the internal CRM on its own, and the
questionnaire data is what M9b's matching needs to be worth building.
`investor_categories` is seeded as part of M9a, so the extensibility exists from
the first migration even though only two categories start enabled.

## Confirmed

- **Accredited investors only, today** — modeled as an extensible category
  registry, not an assumption in code. See "Investor categories are
  extensible."
- **Self-certifying** — which settles the exemption as **506(b)**, since 506(c)
  requires verified accreditation. `accreditation_method` defaults to
  `self_certified`; no verification vendor in v1.
- **The public site never shows an offering** — a permanent product rule,
  enforced structurally (no import path from the public site to offering data),
  not merely a 506(b)-era policy that would loosen under 506(c).

## Open questions for John

1. **Are the three pathways separate legal entities**, or tiers within one? It
   changes whether `tier` is a field or a set of `funds` rows.
2. **Does the portal need e-signature and subscription documents in v1**, or is
   the first version read-only with commitments handled offline?
3. **Is the questionnaire self-serve, or filled in by the team on a call?**
   The former needs the external identity class first; the latter can ship
   inside the internal app immediately.
4. **Any near-term plan to enable a second investor category** — a qualified
   purchaser tier, an institutional tier, eventually Reg CF? Nothing blocks on
   the answer since the registry supports it either way, but it affects what
   `requires_verification` defaults should be seeded with.
