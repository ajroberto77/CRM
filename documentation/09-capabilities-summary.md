# Capabilities Summary

Plain-language inventory of what this CRM does today, organized for
side-by-side comparison against another system. Each item is marked:

- **✅ Built & tested** — real code, exercised by the test suite (788 tests
  passing at the time of this audit), and in several cases hands-on verified
  against the running app in this session.
- **⚠️ Partially built** — real code for part of the capability, a documented
  gap for the rest.
- **📋 Designed, not built** — exists only in `docs/` as intent.

## Contacts & companies

- **✅** Track organizations and people with a flexible, org-definable custom
  field system (no migration needed to add a field).
- **✅** Identity resolution via normalized email/phone/messaging-handle
  matching (never by name) — `contact_channels`.
- **✅** Automatic materialization of a person/organization the moment an
  interaction references them, with a "derived → confirmed" promotion the
  instant a human edits the record.
- **✅** An emergent, computed "type" for any org/person — LP, GP, portfolio
  company, board member, etc. — derived live from its relationship graph,
  never a stored classification column (added this session).
- **⚠️** No fuzzy/duplicate-detection review workflow, no built-in
  conflicting-information resolution (see `02-data-model.md`).

## Relationships

- **✅** Dated, role-bearing relationships between any two records (person ↔
  organization, organization ↔ fund, etc.) — one record can be an LP in one
  fund, a co-investor on a deal, and a portfolio company of another, all at
  once, with one interaction history.
- **✅** Hierarchical rollups (ultimate parent company; investment-relationship
  rollup), cycle-checked at write time.
- **✅** A generic role-based filter for saved views/lists ("organizations that
  are portfolio companies") — added this session.

## Interaction log & derived-CRM bet

- **✅** `core.interactions` as the primitive the rest of the CRM is derived
  from — the core architectural bet (see `01-architecture.md`).
- **⚠️** The mechanism for turning an *inbound* email/message into an
  interaction row **does not yet exist as a live pipeline** — see below.

## Scheduling automation

- **✅** LLM-based extraction of meeting requests from inbound email text,
  with a strict-schema validator and a one-retry repair loop.
- **✅** A real approval queue (manual/confidence-threshold/auto modes,
  trusted-sender auto-accept) before any calendar write happens.
- **✅** Calendar write via Microsoft/Google, with DST-safe timezone-window
  computation.
- **⚠️** The pipeline is fully built and tested, but **has no live inbound
  trigger** today — nothing yet creates the `interaction` rows it consumes
  (see "Mail," below).

## Messaging channels (Signal, Telegram)

- **✅** Two-way linking of a user's own Signal number/Telegram handle.
- **✅** Exact-vocabulary approve/decline commands against pending scheduling
  proposals, with quote-based targeting (no LLM anywhere in this path).
- **⚠️** Email is a documented-but-unbuilt third channel — no
  `server/channels/email.py` exists.
- **⚠️** Inbound Signal/Telegram text that *isn't* approve/decline is
  currently silently ignored — it is not logged as a CRM interaction. This is
  the single largest concrete gap this audit found.

## Mail/calendar/contacts sync (Microsoft 365, Google)

- **✅** OAuth linking (PKCE) for both providers.
- **✅** Contact delta sync (Microsoft Graph delta query; Google People API
  sync tokens with automatic full-resync recovery on token expiry).
- **✅** Calendar read/create/update against both providers.
- **⚠️** **No mail sync exists at all** — no `server/providers/mail.py`, no
  adapters, no job. `core.sync_cursors`' own schema comment anticipates this
  ("a future synced resource (mail, in a later milestone) needs no migration
  here") but it hasn't been built.

## Semantic search

- **✅** Embeddings over interaction bodies (chunked, async job, owner-scoped
  visibility per safety rule 10), queried via a dedicated search endpoint and
  surfaced in the command palette (Cmd/Ctrl+K).

## LLM routing

- **✅** Five providers (Ollama, OpenAI, Anthropic, Gemini, a self-hosted
  Claude Code bridge), a configurable per-org fallback chain, and a
  repair-retry loop for malformed structured output.
- **✅** Ollama-specific Wake-on-LAN for a local/self-hosted model host.

## Asset management vertical (`modules/funds`)

- **✅** Funds, commitments, investment accounts (trusts/LLCs/IRAs/SPVs), GP
  roles, public/private target classification with ticker/exchange tracking
  (added this session), and a prospective-target ("evaluating") vs. actual
  portfolio-company distinction (also added this session).
- **✅** A per-vertical dashboard (funds/commitments/committed-capital by
  status) and nine seeded default saved views (Portfolio companies, LP
  organizations, GP team, etc.) — both added this session, both fully
  registry-driven (no per-vertical frontend code).

## Investor portal / LP compliance (`modules/investor_portal`)

- **✅** Investor classification (categories, pathways, profiles),
  questionnaires with versioned responses, mandate derivation (with
  documented fail-safe behavior on skipped questions).
- **✅** E-signature across four vendors (DocuSign, Dropbox Sign, PandaDoc,
  Adobe Sign) and two real write-time compliance gates (a commitment can't
  close without an executed subscription agreement; an investment account
  can't activate without required AML/KYC/tax documents on file, with the
  same skip-safe fail-closed behavior).
- **📋** The actual "portal" — external-facing gated login, self-serve
  questionnaire delivery, matching/offerings between LPs and deals, a public
  marketing site — is **entirely unbuilt**, design intent only.

## Platform mechanics (less visible, but load-bearing for a merge)

- **✅** Full field-level audit log of every write (`core.events`), with
  actor attribution and at-least-once redelivery for anything that failed to
  dispatch synchronously.
- **✅** Postgres Row-Level Security multi-tenancy — the org boundary is
  never an application-level filter.
- **✅** A background work queue (claim/lease/retry/dead-letter) alongside two
  older standalone poll loops (contacts sync, messaging commands) that
  predate it and haven't been migrated onto it.
