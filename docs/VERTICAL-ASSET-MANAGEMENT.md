# Vertical — asset management

How this platform serves an asset-management firm without becoming a
single-purpose product. Read `docs/DESIGN.md` first; this describes the layer
on top of it.

The requirement: track **investors** (including who invested in which fund),
**companies**, **people at companies**, and **other investors** — with
flexibility to go beyond that later.

## The rule that makes this work

> **"Investor" is not an entity type. It is a role one organization plays
> relative to one fund, over a period of time.**

The tempting design is an `investors` table and a `portfolio_companies` table.
It breaks on contact with reality, because in this business the same legal
entity routinely wears several hats at once:

- Brightline Capital is an **LP in Fund II**, a **co-investor** on one deal,
  and — after a secondary — a **counterparty** on an LP transfer.
- A family office is an **LP** and the **owner** of a company you also hold.
- Another GP's fund is a **co-investor**, and later the **buyer** of a portfolio
  company.
- A portfolio company founder later becomes an **LP** in the next fund.

With role-bearing relationships, all of those coexist on one record and the
history is intact. With separate tables, that is four duplicate records of the
same organization, four separate interaction histories, and a relationship graph
that quietly stops being a graph.

So: **one `organizations` table, one `persons` table, and every hat is an
association carrying a role and a date range.**

## What is core, what is a module

| Layer | Contents | Owner |
|---|---|---|
| **Core (tier 1)** | `persons`, `organizations`, `interactions`, `contact_channels`, `associations`, `deals`, `tasks`, `notes`, `documents`, `metric_facts` | Platform |
| **Module `funds`** | `funds`, `commitments`, `capital_transactions`, `positions`, `investments` | Vertical |
| **Tier 2** | user-defined objects in `records(object_id, org_id, data jsonb)` | Users, at runtime |

Core knows nothing about funds. The vertical is a directory under `modules/`
with a manifest declaring its entities, its association roles, its UI slots and
its permission scopes — the EspoCRM shape from `docs/DESIGN.md`. A different
vertical later is a different module, not a fork.

## Three additions core needs

These are generic capabilities the vertical happens to demand first. They belong
in core because any CRM wants them, and all three are expensive to retrofit.

### 1. Associations carry a role and a date range

`associations(from_type, from_id, to_type, to_id, role, attributes jsonb,
valid_from, valid_to, created_at)`.

Dating the relationship is what makes history answerable — *was* an LP in Fund I
but not Fund II, *was* CFO until March, *sat* on the board during the
investment. `attributes` carries the few role-specific extras (title, board
seat type, ownership percentage) that do not deserve columns.

This replaces the employment table, the board-seat table, the co-investor table
and the LP-relationship table that would otherwise each get written separately —
rule R1, applied to the domain rather than to providers.

**Roles are a closed vocabulary per module**, declared in the manifest and
validated on write. Free text drifts (`works_at` / `works at` / `employee_of`)
and silently stops matching, the same failure Cal hit with event categories.

Core roles: `works_at`, `board_member_of`, `advisor_to`, `introduced_by`,
`owns`, `vendor_to`.
The `funds` module adds: `lp_in`, `gp_of`, `portfolio_of`, `co_investor_in`,
`lender_to`, `acquirer_of`.

### 2. `metric_facts` — periodic data

`metric_facts(subject_type, subject_id, period_start, period_end, metric_key,
value_numeric, value_text, currency, source, confidence, document_id)`.

Event-shaped and period-shaped data are different tables. "Acme's Q3 2026
EBITDA" is not an event and cannot live in `interactions`; it is also not a
property of the company, because next quarter there is another one. Nor can it
live in `custom jsonb` — you cannot chart a JSONB key or run a range query over
it without an expression index per metric.

Uses here: portfolio-company KPIs, borrower financials, fund NAV by quarter, LP
AUM. `source` and `confidence` mean an AI-extracted figure from a PDF is
distinguishable from one a human typed, and `document_id` points back at what it
was extracted from.

Retrofitting this is the same class of pain as retrofitting the interaction log.
Build it in M2 alongside `interactions`.

### 3. `documents`

`documents(id, org_id, subject_type, subject_id, kind, filename, storage_key,
mime, bytes, uploaded_by, valid_from, valid_until, status)`.

The vertical needs subscription agreements, side letters, KYC/AML evidence, tax
forms, and quarterly reports. `valid_until` matters more than it looks: a KYC
document that expires is a record whose staleness must **gate an action**, not
just show a warning. That is the same "gates raise, never no-op" rule from
`CLAUDE.md`, applied to compliance.

## The specific asks

### Who invested in which fund

A commitment, not an association — it has an amount, a currency, close dates and
a lifecycle, and it is queried from both directions.

```
funds(id, org_id, entity_org_id, name, vintage_year, strategy, currency,
      target_size, hard_cap, first_close_at, final_close_at, status)

commitments(id, org_id, fund_id, investor_org_id, investor_person_id,
            amount, currency, committed_at, status, side_letter_document_id)
```

Two details that are deliberate:

- **`funds.entity_org_id` points at an `organizations` row.** A fund *is* a
  legal entity. Making it one means another GP's fund — a co-investor, or the
  buyer of one of your companies — is the same shape as your own, sits in the
  relationship graph, and has its own interaction history. Your funds are
  organizations flagged internal; theirs are not. This is what "other investors"
  costs: nothing.
- **`investor_org_id` OR `investor_person_id`.** An institutional LP is an
  entity with contacts hanging off it; an individual LP is a person. Both commit.

Contacts at an LP are `persons` with a `works_at` association to the LP
organization — so "everyone we know at Brightline" and "everyone at Brightline
who is on a deal we co-invested in" are the same query shape.

### Companies, and people at companies

A portfolio company is an `organizations` row with a `portfolio_of` association
to a fund, dated from close to exit. People at it are `persons` with dated
`works_at` associations carrying their title.

The post-close position is module data, because ownership and cost basis are
neither a relationship nor a periodic fact:

```
investments(id, org_id, fund_id, company_org_id, deal_id,
            invested_at, exited_at, cost_basis, currency,
            ownership_pct, security_type, current_valuation, valued_at)
```

`deal_id` links back to the generic pipeline: a deal is the opportunity, an
investment is what it becomes when it closes. The pipeline stays core; only the
post-close position is vertical.

### Other investors

Co-investors are `organizations` with `co_investor_in` associations to the
investment (or the deal, pre-close). Because they are ordinary organizations,
they accumulate interaction history, people, and their own relationship
strength — so "which co-investors have we actually worked with, and who owns
those relationships" is answerable without a second CRM.

### Money

```
capital_transactions(id, org_id, commitment_id, kind, amount, currency,
                     effective_at, notice_document_id, status)
```
`kind` ∈ `call | distribution | fee | expense | adjustment`.

Positions are **derived and cached in real columns**, never in JSONB — they are
high-churn, and the rule in `CLAUDE.md` is explicit about that. Paid-in,
distributed, NAV, and the LP-level performance figures (IRR, MOIC, DPI, RVPI)
recompute from the transaction ledger on write and on demand.

**What this deliberately is not:** a fund accounting system. No waterfall
engine, no equalisation, no ILPA report generator, no general ledger. Those are
a different product, and a firm that needs them has an administrator who already
runs one. What is here is enough to answer "what has this LP committed, paid and
received, and what is it worth" — which is a CRM question. If real fund
accounting is ever wanted, it is another module against the same ledger.

## Where the relationship intelligence pays off

The interaction-derived model from `docs/DESIGN.md` is unusually well suited to
this vertical, because fundraising *is* relationship work:

- **Warm paths into an LP** — who on the team has actually corresponded with
  them, and how recently. Computed from `interactions`, across every connected
  mailbox.
- **Re-up risk** — an LP in Fund II who has gone quiet three months before Fund
  III opens. Deal rotting, applied to relationships instead of deals.
- **Coverage gaps** — an LP with a large commitment and no interaction in a
  quarter, or one owned by a partner who has left.
- **Founder and operator networks** — people at portfolio companies are already
  `persons` with full history, so "who do we know who has run a
  Series-B-stage logistics business" is a query, not a memory exercise.

None of this is computable from a contacts table, which is why the interaction
log was the one architectural decision flagged as expensive to reverse.

## Keeping the flexibility

Three mechanisms, in increasing order of how much they cost:

1. **Custom fields** — a JSONB `custom` column on every core entity, governed by
   the `custom_fields` registry, with index promotion when a field needs range
   queries. No code, no migration.
2. **User-defined objects** — tier 2's generic `records` table. A new object
   type at runtime with no DDL, so a firm that wants to track, say, placement
   agents or LP advisory committee seats can, without a release.
3. **A new module** — a directory with a manifest. This is how a second vertical
   arrives, and it is the only one of the three that needs code.

The test for whether the layering is honest: **core must never mention a fund.**
If `core/repository.py` or the generic API ever branches on a fund-specific
concept, the seam is in the wrong place — the same rule as R2, which is why
`no-dupes` greps for provider names and should grow to grep for module-owned
entity names in core.

## The LP portal — decided, see `docs/INVESTOR-PORTAL.md`

The shape below stands and is now a planned workstream (M9a–c) rather than a
deferred question, extended with investor classification and a public site.
Confirmed: accredited investors only today, modeled as an extensible category
registry rather than a hard-coded assumption; self-certifying, which settles
the offering exemption as 506(b); and the public marketing site **never**
shows an offering — a permanent product rule enforced structurally (no import
path from the public site to offering data), not merely a policy that would
loosen if the exemption ever changed. Qualification gates the query, not the
navigation, and the dated interaction log is part of the evidence that a
relationship predates an offering.

An investor portal is a **second identity class**, not a permission level. LPs
are external, see only their own commitments and documents, and must never
appear in the internal user directory or the relationship graph as colleagues.

The seam already exists: `auth.identities` separates identity from tenant, so an
external identity is a new `kind` on that table plus a grant-based access path
(`document_grants`, `commitment_grants`) rather than a role. What it must **not**
become is another visibility level in `core/permissions.py` — mixing external
parties into the internal role model is how a "share with LP" feature turns into
an org-wide data leak.

Deferred, but the shape is decided, and nothing in M1–M7 should foreclose it.
