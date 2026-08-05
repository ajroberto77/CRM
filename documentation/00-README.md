# Documentation — Full Project Audit

This folder is a from-the-code audit of this repository, written to support a
comparison against a related sibling project (front-end/UI-focused here vs. a
data-collection-and-other-capabilities focus there) toward figuring out how the
two should integrate. Unlike `docs/` at the repo root — which is the *design*
record (intent, rationale, decisions as they were made) — this folder describes
what is *actually implemented*, verified by reading the real source, running the
test suite, and in several places by driving the running application directly.

Where something in `docs/` (a design doc) is aspirational and not yet built, that
gap is called out explicitly here rather than silently repeated as fact — `CLAUDE.md`
itself warns that design docs "describe intent, not reality."

## How this folder is organized

| File | Covers |
|---|---|
| `01-architecture.md` | The load-bearing mechanisms: the entity registry, the generic repository/CRUD path, the permission model, RLS/multi-tenancy, the event bus, the module system, the six provider-dispatch axes. |
| `02-data-model.md` | Every registered entity and every association role, core and module, with the table each maps to. |
| `03-api-reference.md` | The full HTTP surface — the generic records API plus every hand-written route (auth, accounts, channels, settings, search, proposals). |
| `04-llm-subsystem.md` | The LLM router, its five provider adapters, the fallback-chain/repair-retry logic, the one extraction task (scheduling), and embeddings/semantic search. |
| `05-messaging-channels.md` | Signal/Telegram/email dispatch, the exact-match command grammar, and how an inbound message becomes an interaction. |
| `06-sync-and-jobs.md` | Mail/calendar/contacts sync against Microsoft 365 and Google, the job queue, and the scheduling-extraction → approval-queue → calendar-write pipeline. |
| `07-modules.md` | The two installed modules (`funds`, `investor_portal`) and the e-signature dispatcher — what's built and tested vs. still just documented intent. |
| `08-frontend.md` | The React/TypeScript frontend: routing, auth, settings, the command palette, shared utilities, and the design-token system. |
| `09-capabilities-summary.md` | A plain-language inventory of what this CRM does today, organized for side-by-side comparison against another product. |
| `10-integration-notes.md` | Open questions and a framework for the cross-project integration decision — filled in once the sibling project is identified. |

## Headline facts, for orientation

- **Stack**: Python 3.11+/FastAPI backend, deliberately stdlib-heavy (only real
  runtime deps: FastAPI/uvicorn, psycopg2, pgvector); React + TypeScript + Vite
  frontend with almost no dependencies beyond React itself.
- **Database**: one PostgreSQL database (`crm`), four logical schemas —
  `core`, `sync`, `jobs`, `ai` — multi-tenant via Postgres Row-Level Security,
  never an application-level `WHERE org_id = ...` filter.
- **Core architectural bet**: the CRM is *derived from an interaction log*, not
  a system of record people type into. `core.interactions` is the primitive;
  `organizations`/`persons` materialize from it (`is_derived` flag) and get
  promoted the moment a human touches one.
- **Generic-core discipline**: a small number of dispatch points (registry,
  repository, six provider axes) mean a new entity, a new relationship role, or
  a new provider is additive — a registration, not a new code path. This is
  enforced, not just intended: `registry.verify()` runs at startup and a
  tokenized R6 scan in the test suite fails the build if vertical vocabulary
  (a fund, a commitment) leaks into `server/core/`.
- **Vertical**: the primary vertical is asset management (`modules/funds`),
  with a nascent LP-portal module (`modules/investor_portal`) layered on top —
  both are ordinary modules, not special-cased core code.
