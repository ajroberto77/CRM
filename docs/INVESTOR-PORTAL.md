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

## The constraint that dictates the architecture

**506(b) prohibits general solicitation.** Offerings cannot be advertised
publicly, and may only be shown to investors with a pre-existing substantive
relationship with the sponsor. That is not a compliance footnote to add later —
it decides what the software is allowed to render to whom:

| Surface | May contain | Must not contain |
|---|---|---|
| Public site | Who the firm is, thesis, team, past portfolio, "register interest" | Any live offering, terms, allocation, or deal-specific material |
| Gated portal | Live offerings, terms, documents, allocations | — visible only to investors who are qualified **and** related |

Two consequences worth building in rather than retrofitting:

1. **The relationship must be evidenced and dated.** The questionnaire is not
   only classification — completing it, and the interactions preceding it, are
   part of the record establishing that the relationship predates the offering.
   The platform already has a dated interaction log; this makes it load-bearing.
2. **Qualification state gates rendering, not just navigation.** An offering is
   withheld at the query layer, never merely hidden in the UI.

If the firm ever moves to **506(c)**, general solicitation becomes permissible
but accreditation must be **verified** rather than self-certified — a different
`verification_method` and evidence requirement, not a different architecture.
Both are modelled from the start.

*Not legal advice.* The design should be reviewed by the firm's securities
counsel before launch, and the rules above encoded as configuration rather than
assumptions baked into code.

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
investor_profiles(id, org_id, subject_type, subject_id,
                  status, tier, accreditation_method, accredited_at,
                  accreditation_expires_at, verified_by, relationship_since,
                  min_check, max_check, custom)

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

A match is a **ranking and a suggestion**, never an automatic grant. Under
506(b) the decision to show an offering to a specific investor is a judgment the
firm makes, so the system proposes and a human grants — recorded in
`offering_grants` with who granted it and why. That is the same
`proposed_changes` discipline already in the platform, and here it is also the
audit trail.

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
allocations.

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

## Open questions for John

1. **506(b) or 506(c)?** It decides whether the public site may mention live
   offerings at all, and whether self-accreditation is sufficient.
2. **Who verifies accreditation** — self-certification, a third-party service,
   or documents reviewed in-house? This sets the evidence the portal must store.
3. **Are the three pathways separate legal entities**, or tiers within one? It
   changes whether `tier` is a field or a set of `funds` rows.
4. **Does the portal need e-signature and subscription documents in v1**, or is
   the first version read-only with commitments handled offline?
5. **Is the questionnaire self-serve, or filled in by the team on a call?**
   The former needs the external identity class first; the latter can ship
   inside the internal app immediately.
