# CRM — Project Guide for Claude

A self-hosted, multi-user, modular CRM on PostgreSQL. Tracks contacts, does
scheduling, routes across multiple LLM providers, is driven on the fly from
Signal or Telegram, and syncs contacts with Microsoft 365 and Google.

Read this file before making changes. Read `docs/DESIGN.md` before making
architectural ones.

## The five rules

These override convenience, style preference, and any instinct to "just add a
small helper." They exist because this platform is explicitly **one code base**
— the stated goal is that similar things are never developed twice.

### R1 — One implementation per capability

Before writing anything that fetches, retries, dispatches, normalizes, renders a
list, formats a date, or talks to a provider, **search for the existing one**. If
something 80%-similar exists, extend it with a parameter. Never fork it.

If you find yourself writing a second function whose docstring would read like
one that already exists, stop — that is the rule firing.

### R2 — Generic core, name-prefixed adapters

Orchestration files carry no provider name and never import a provider-specific
module directly, only through a single dispatch point. Provider-specific files
are prefixed with the provider's name (`microsoft_calendar.py`,
`google_contacts.py`, `signal_cli.py`).

Adding a provider is **one new `<provider>_*.py` file plus one `elif` in the
dispatcher**. If adding a provider requires touching anything else, the seam is
in the wrong place.

### R3 — Five axes, five dispatchers

The platform has exactly five provider axes. Each has exactly one dispatch
module, and **no other file may branch on provider identity**.

| Axis | Dispatcher | Adapters |
|---|---|---|
| LLM | `server/llm/router.py` | ollama, openai, anthropic, gemini, claudecode |
| Mail | `server/providers/mail.py` | microsoft, google |
| Calendar | `server/providers/calendar.py` | microsoft, google |
| Contacts | `server/providers/contacts.py` | microsoft, google |
| Messaging | `server/channels/dispatch.py` | signal, telegram, email |

### R4 — One CRUD path

Entities are declared once in `server/core/registry.py` and get repository, REST
routes, and list/detail UI generically. **A new entity is a registry entry, not
a new stack of files.** Hand-written CRUD for a core entity is a bug.

### R5 — One of each singleton

One `web/styles/tokens.css` — the only file in `web/` or `server/` permitted to
contain a literal color, font stack, radius or shadow (`docs/` is exempt; it
quotes the palette deliberately). One `server/config.py`, one connection pool,
one retry module, one normalizer per value type.

Normalizers especially: `server/core/identity.py` owns email/phone/handle
normalization. Import it. Never reimplement it, never inline a `.lower().strip()`
on a value that has a normalizer. (This is CATO's `normalize_cik` discipline —
that codebase learned it the hard way.)

## Stack

- **Backend** — Python 3.11+, FastAPI. Deliberately stdlib-heavy: the only
  runtime dependencies are FastAPI/uvicorn, psycopg2, and pgvector. Both sibling
  projects this descends from are stdlib-only by design; keep it that way.
- **Database** — PostgreSQL, one database `crm`, logical schemas `core`, `sync`,
  `jobs`, `ai`.
- **Frontend** — React + TypeScript + Vite.

## Directory layout

```
server/
  config.py                 # single source of truth for settings
  db/pool.py schema.py      # table dicts -> CREATE + ALTER migration
  core/registry.py          # entity registry (R4)
  core/repository.py        # THE generic CRUD path + visibility predicates
  core/identity.py          # the one normalizer (R5)
  core/events.py            # the event bus -- the extension seam
  core/proposals.py         # the one approval queue
  core/trust.py             # the one definition of "trusted"
  api/                      # FastAPI app, auth, saved views
  llm/                      # router + chain + per-provider adapters
  extraction/               # one file per task; the router stays task-agnostic
  providers/                # mail/calendar/contacts dispatchers + adapters
  channels/                 # messaging dispatch + adapters + command grammar
  jobs/                     # work queue, workers, sync jobs
modules/                    # optional feature modules (manifest + metadata)
web/styles/tokens.css       # the design system
docs/                       # DESIGN.md, COMPETITIVE-ANALYSIS.md, SOURCE-PATTERNS.md
```

## Database conventions

- **Never guess** table or column names. Read the authoritative source —
  `server/db/schema.py`, or the live schema via `psql`. Design docs describe
  intent, not reality.
- **psycopg2, explicit cursor pattern**, no wrapper abstractions.
  `execute_batch` for bulk inserts.
- **Every table carries `org_id`.** RLS is enabled with `FORCE ROW LEVEL
  SECURITY`, and every composite index puts `org_id` **first**. Missing that
  leading column is two orders of magnitude slower, not a rounding error.
- The app's database role must never have `BYPASSRLS` and must never be a
  superuser.
- Connections run through a transaction-mode pooler, so tenant context is set
  with `SET LOCAL` **inside** the transaction — never plain `SET`.
- Record-level visibility (own/team) is **not** RLS. It is predicate injection at
  the single choke point in `core/repository.py`, where it can be read, indexed,
  and debugged.

## Safety rules

These are not stylistic. Each corresponds to a defect that reached a running
deployment in a sibling project; `docs/SOURCE-PATTERNS.md` records which.

1. **No LLM in any action path.** Commands from a messaging channel are matched
   against an exact vocabulary. A real write is only ever triggered by
   exact-match code, never by anything resembling judgment.
2. **Config is read-merge-write.** Never load a subset and write it back as the
   whole file. Provide an explicit clearable-key set, or fields can never be
   blanked.
3. **Gates are enforced at the single execution choke point, by construction**,
   and **raise** rather than no-op. A no-op lets the caller mark the work done.
4. **A transient failure must not lose data.** Never commit a delta cursor past
   a message whose processing failed. Retain bodies; provide a reprocess path
   restricted to failed items so a retry cannot duplicate.
5. **No per-process latches on recoverable external state.** A host that was
   unreachable once must be re-checked.
6. **Validate every field** at the layer where a `ValueError` triggers the
   extraction repair-retry — including types. A bare string where a list is
   expected once became one calendar attendee *per character*, each sent as a
   real meeting invite.
7. **Timezone bounds are computed, never string-formatted.** Provider query
   params are UTC; a "local day" window must be converted, and local-midnight +
   one day is wall-clock arithmetic so DST days span 23 or 25 hours.
8. **Unclassifiable input fails safe** to the approval queue. A null category
   never matches an auto-accept rule.
9. **Destructive paths default off** behind an explicit setting, with approval
   defaulting to `manual`.

## Operating rules

1. **Read before you change.** Confirm how things actually work; never guess
   config keys, table names, or paths. If ambiguous, stop and ask.
2. **Real fixes only.** No temporary workarounds or throwaway scripts. Every fix
   is a permanent change in actual source files.
3. **Surgical edits over rewrites.**
4. **No hardcoded values.** Settings come from `server/config.py`; colors come
   from `tokens.css`.
5. **Show diffs and explain changes before committing. Never commit secrets.**
   Secrets live in env, never in the database and never in a config file.

## Sibling repositories — read-only

`Cal`, `JA`, and `CATO` are reference only. **Never edit them.** No shared
packages, no submodules. Patterns are ported into this codebase and then owned
here.

Read them at the right ref — `main` is stale or empty in both of the first two:

- **Cal** → branch `claude/calendar-coordination-office365-9n6jq3`
- **JA** → branch `claude/db-modularization`, under `prototype/`
- **CATO** → default branch

See `docs/SOURCE-PATTERNS.md` for what comes from where.

## Subagents

Project agents live in `.claude/agents/`:

- `no-dupes` — enforces R1–R5 on every diff. Run before committing.
- `schema-guardian` — any change touching the PostgreSQL layer.
- `ui-reviewer` — any new panel/component or stylesheet.
