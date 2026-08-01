# CRM — Architecture & Design

The design of record for this platform. `CLAUDE.md` is the short operating
guide; this is the reasoning behind it. `docs/COMPETITIVE-ANALYSIS.md` holds the
evidence for the market-derived decisions, and `docs/SOURCE-PATTERNS.md` records
what is ported from the sibling projects and why.

## Context

`ajroberto77/CRM` is an empty repository — no commits, no branches. The goal is
a **self-hosted, multi-user, modular CRM** that tracks contacts, schedules like
Cal, routes across multiple LLMs, is driven on the fly from Signal or Telegram,
syncs contacts with Microsoft 365 and Google, stores everything in one central
PostgreSQL database, and wears Cal's visual identity.

Three sibling repos are **read-only reference** — no edits, no shared packages,
no submodules. Their value is proven patterns and hard-won operational
knowledge, ported into one codebase and then never written twice.

| Repo | Read this ref | Contributes |
|---|---|---|
| `Cal` | **`claude/calendar-coordination-office365-9n6jq3`** — *not* `main` | Colors + layout, LLM router **with provider fallback chain**, four-axis provider dispatch, Graph OAuth/mail/calendar, scheduling extraction, approval modes, trusted-sender auto-accept, dashboard auth, and six live-deployment bug fixes. 288 tests. |
| `JA` | **`claude/db-modularization`** (2026-07-31), under `prototype/` — *not* `main`, a bare README | Signal messaging layer, the two-way command loop, Gmail **and** Outlook providers, both calendar syncs, schema-constrained JSON decoding, contact aliases |
| `CATO` | working tree | PostgreSQL conventions, config-singleton pattern, coordinator/worker queue, subagent-enforced project rules |

Both sibling repos keep their real work on branches. `main` in each is stale or
empty — reading `main` would have produced a plan built on the wrong code.

## The decisive design finding

Competitive analysis of Twenty, Attio, Salesforce, HubSpot, EspoCRM, SuiteCRM,
Odoo, Erxes, Affinity and Clay produced one conclusion that changes the
foundation:

> **The CRM should be a derived view over an interaction log, not a system of
> record that people type into.**

Affinity and Clay create person and company records automatically from mail and
calendar traffic, then compute relationship strength, warm-intro paths and
"going cold" signals. The insight isn't auto-import — it's that *the failure
mode of every typed-in CRM is that nobody types*. The schema consequence is
large: the primitive is **`interactions`**, and persons/organizations are
materialized from it with an `is_derived` flag and a promotion step when a human
touches one.

Not cheaply reversible — retrofitting an interaction log under an existing
contacts model is the hard version. It also fits this platform better than the
products it was observed in: Cal and JA already classify mail, and
Signal/Telegram add more interaction streams. **Adopted.**

## The No-Duplicate-Work Rule

*"You will not develop similar things twice — I want one code base."* Five
clauses in `CLAUDE.md`, policed by a `no-dupes` subagent (modelled on CATO's
`scope-cop`).

> **R1 — One implementation per capability.** Before writing anything that
> fetches, retries, dispatches, normalizes, renders a list, or talks to a
> provider, find the existing one. If something 80%-similar exists, extend it
> with a parameter. Never fork it.
>
> **R2 — Generic core, name-prefixed adapters.** (Cal's convention, verbatim.)
> Orchestration files carry no provider name and never import a provider module
> directly — only through a single dispatch point. Adding a provider = one new
> `<provider>_*.py` + one `elif`.
>
> **R3 — Five axes, five dispatchers.** Nothing else branches on provider
> identity: `llm/router.py`, `providers/mail.py`, `providers/calendar.py`,
> `providers/contacts.py`, `channels/dispatch.py`.
>
> **R4 — One CRUD path.** Entities are declared once in a registry and get
> repository + REST + list/detail UI generically.
>
> **R5 — One of each singleton.** One `tokens.css` (the only file with a literal
> color), one `config.py`, one pool, one retry module, one normalizer per value
> type — CATO's `normalize_cik` discipline: *import it, never reimplement it*.

Three places R1 pays off immediately, all load-bearing:

- **One `proposed_changes` queue** serves Cal's approval modes, AI-suggested
  field writes, and provider-sync conflicts. HubSpot ships this exact shape for
  AI. Building it three times would be the first violation.
- **One event bus** (`record.created`, `record.updated` with field diffs) is the
  extension seam. A workflow engine, if it exists, is the bus's first
  *consumer*, not a parallel mechanism.
- **One definition of "trusted"** — Cal's `senders.py` is the model: one
  function over two tables *"so there is exactly one place where trusted is
  defined."*

## Operational lessons that become requirements

Cal's office365 branch fixed six confirmed defects found by auditing a running
deployment. Each is a class of bug this CRM will otherwise reproduce, so each
becomes a rule rather than a note.

1. **Read-merge-write config, never read-subset-write-whole.**
   `set_public_config()` loaded only the 8 generic keys and wrote that back as
   the entire config, destroying all 15 LLM keys on every save — invisible while
   running, because reload only ever *set* env vars and never cleared them, so
   it surfaced after the next restart as a silent revert to defaults. Also needs
   an explicit **clearable-key set**, or fields can never be blanked.
2. **Enforce gates at the single execution choke point, by construction, and
   raise rather than no-op.** The approve button bypassed
   `calendar_write_enabled` entirely and wrote to real calendars while writes
   were disabled. A no-op would have let the caller then mark the event
   scheduled.
3. **A transient LLM outage must not permanently lose data.** Extraction failure
   returned normally, so the poller committed its delta cursor and the message
   left the provider's stream forever. Requires: retain the body, and a
   reprocess path restricted to `needs_review` so a retry cannot duplicate.
4. **No per-process latches on recoverable external state.** A wake-host check
   latched a flag and returned early forever, so a machine that slept mid-day
   was never woken again.
5. **Validate every field at the layer where a `ValueError` triggers the
   repair-retry** — not just the obvious ones. `duration_minutes="90"` raised
   deep in `timedelta`; `"2026-13-45"` passed the format regex; and
   `attendees="alice@example.com"` was exploded by `list()` into one attendee
   *per character*, **each sent to Graph as a required attendee on a real
   meeting invite**. Type validation is a safety control, not hygiene.
6. **Timezone bounds are computed, never string-formatted.** See below.

## Microsoft Graph integration — what Cal actually proved

`microsoft_graph_client.py` is the model: public client with **PKCE, no client
secret**, against the **`/common` tenant** so personal *and* work/school
accounts both work (JA was `/consumers`-only). Retries 429/503/504 honoring
`Retry-After`. Refresh tokens are rotated and persisted when Microsoft returns a
new one. Credentials are stored provider-agnostically (`credential_type` +
`payload_json`), so the Graph module is what knows the OAuth payload shape —
`accounts.py` does not.

Two findings to carry over verbatim:

- **`Prefer: outlook.timezone` only affects how Graph *renders* times in the
  response — it does not reinterpret `startDateTime`/`endDateTime` query
  params**, which Graph treats as UTC when they carry a `Z`. Hardcoding `Z`
  shifted the `calendarView` window by the account's whole UTC offset (4–5h for
  New York), missing events after 8pm local and pulling in the previous
  evening's — feeding the wrong day to the create-vs-update heuristic. The
  window must be the account's **local day converted to UTC**, and
  local-midnight + `timedelta(days=1)` is wall-clock arithmetic so DST days
  correctly span 23 or 25 hours.
- **Raw MIME (`request_mime`) is the only documented way to guarantee
  caller-set `In-Reply-To`/`References` survive** — Graph's JSON `createReply`
  isn't documented to preserve them. Threading depends on this.

**Still unverified in Cal, so treat as unproven here:** Graph's event
`start/end.timeZone` defaults to expecting *Windows* zone names ("Eastern
Standard Time"), not the IANA names used everywhere else. Cal sends
`Prefer: outlook.timezone="<IANA>"` to work around it but has never exercised it
against a live account. **Verify a created event lands at the correct wall-clock
time before trusting any unattended calendar write.**

For contact sync the research adds provider gotchas that shape the schema:

- **Graph** `/me/contacts` covers **only the default folder** — enumerate
  `contactFolders` and delta per folder. Delta tokens have no fixed TTL
  (eviction from an internal cache), so `410 resyncRequired` is a *normal path*.
  `@odata.deltaLink` is opaque — never reconstruct it. IDs are unstable without
  `Prefer: IdType="ImmutableId"`.
- **Google People** `syncToken` expires at **7 days**; expired returns 410
  `EXPIRED_SYNC_TOKEN`. The first page of a full sync draws on a **separate,
  fixed, non-increasable quota** — a bug causing mass re-syncs locks you out.
  Deletions appear **only in incremental responses** as `deleted: true`, so a
  full resync makes them invisible and requires diffing against the local set.

Hence `sync.external_links` stores `(entity_type, entity_id, provider,
account_id, folder_id, external_id, etag, delta_token, token_acquired_at,
last_full_sync_at)`; token loss is a tested routine, not an incident; matching
is on normalized email set first, then `(normalized name + domain)`, **never
name alone**.

**Ship one-way (provider → CRM) first** with tombstone handling. Write-back
comes later, per-field, through `proposed_changes`. Naive bidirectional sync is
the documented trap.

## Design system — extracted from Cal

`dashboard.html`'s `:root` block, verbatim → `web/styles/tokens.css`.

```css
:root{
  --bg0:#ffffff; --bg1:#f7f8fa; --bg2:#eef0f3; --bg3:#e3e6ec;
  --border:#e2e5ea;
  --navy:#0d1b2a;
  --text0:#1a2233; --text1:#333b4d; --text2:#5b6472; --text3:#94a0b3;
  --blue:#1b4b78; --blue-light:#3b7cb8;
  --green:#1f9d6b; --yellow:#b8860f; --red:#c94a4a;
  --mono:'JetBrains Mono',monospace;
  --sans:'Inter',sans-serif; --display:'Inter',sans-serif;
}
```

Carried over unchanged: 50px `--navy` titlebar, mono wordmark at
`letter-spacing:.18em`, right-aligned status pill; 190px `--bg1` sidebar with
two-digit mono `nav-num` and a 2px `--blue` active left border; `.card` at
`border-radius:8px` + `box-shadow:0 1px 2px rgba(20,30,45,.04)`; badges tinted
10% over a 30% border; `.btn-primary`/`.btn-sec`/`.btn-danger`; `.field-group`
with 10px uppercase label + 11px `--text3` note; modals over
`rgba(13,27,42,.35)`; the `.test-result` ok/err strip.

Also carried over — a UI *behavior* Cal learned twice: **never rebuild a control
that holds live-fetched state on every page visit, and never show unverified
values as if verified.** Cal's model dropdown had to be fixed three separate
times (wiped live-fetched lists on navigation, reset a verified selection after
save, pre-filled a saved value as though confirmed). Any CRM control backed by a
live provider call inherits this rule.

Additions Cal did not need:

1. **Dark mode** — tokens re-expressed as a semantic layer so
   `[data-theme="dark"]` overrides without touching component CSS.
2. **Density** — Cal's 14px card-row padding is too airy for a 500-row table;
   a `--row-pad` token with comfortable/compact values.
3. **Scoped class names** — CATO's `ui-design-reviewer` documents unscoped
   `.field-row`/`.notice` colliding across stylesheets and silently clobbering
   each other. Every stylesheet prefixes its classes.

### Layout, from Attio/Twenty

- **Left rail:** object nav with **saved views nested under each object** —
  first-class persisted objects with their own filters, sorts, columns and
  grouping, created by users rather than configured by an admin.
- **Center: the table is the app.** Spreadsheet-grade, **inline in-cell editing,
  no edit mode**, keyboard navigation, view switcher (Table / Kanban /
  Calendar). Kanban is the same view grouped by a status attribute.
- **Record page, three regions:** left summary rail (inline-editable
  attributes), center **merged timeline** — the interaction log rendered —
  right related-records blocks.
- **Cmd-K command palette** as primary navigation.
- From Pipedrive, exactly two things: the stage board as the default deal view,
  and **deal rotting** (cards degrade after N days without activity). Highest
  ROI-per-line in CRM UX, and free here because staleness already falls out of
  `interactions`.

## Architecture

One Postgres database `crm`, logical schemas `core` / `sync` / `jobs` / `ai`.
(CATO uses ten databases because it has ten unrelated domains; this is one
domain, so one database and no cross-database JOIN problem.)

```
server/
  config.py                 # single source of truth (CATO cato_config.py shape)
  db/pool.py schema.py      # table dicts -> CREATE + ALTER (Cal db.py, for Postgres)
  core/registry.py          # entity registry: fields, custom-field + index policy
  core/repository.py        # THE generic CRUD path (R4) + visibility predicates
  core/identity.py          # normalize_email/phone/handle -- the one normalizer (R5)
  core/events.py            # the event bus -- the extension seam
  core/proposals.py         # the one approval queue (AI + sync + scheduling)
  core/trust.py             # is_trusted()/trust_reason() over people + services
  api/app.py auth.py views.py
  llm/router.py chain.py openai_compatible.py ollama.py anthropic.py gemini.py claudecode.py
  llm/http_retry.py
  extraction/scheduling.py enrichment.py     # task owns prompt+schema+validation
  providers/mail.py calendar.py contacts.py oauth.py
  providers/microsoft_*.py google_*.py
  channels/dispatch.py commands.py signal_cli.py telegram.py
  jobs/queue.py workers.py sync_contacts.py poll_mail.py
modules/                    # EspoCRM-shaped: manifest + declarative metadata + services
web/  styles/tokens.css + components
```

### Data model

**Two-tier, decided up front** (bolting the second tier on later is expensive):

- **Tier 1 — core entities are real tables**: `interactions`, `persons`,
  `organizations`, `deals`, `tasks`, `notes`, `users`, `pipelines`, `stages`,
  `saved_views`. `persons` and `organizations` carry `source`
  (`human | derived | sync:<provider>`) and `is_derived`, so a typed-in record
  and an observed one are the same shape and a derived record promotes on first
  human edit.
- **Tier 2 — user-defined entities** live in a generic
  `records(object_id, org_id, data jsonb)`, never as runtime DDL.

Each core table also carries `custom jsonb` governed by a `custom_fields`
registry.

**Rejected: per-workspace schemas / runtime DDL (Twenty).** Twenty names schemas
`workspace_${uuidToBase36(workspaceId)}`. Their own tracker documents the cost:
issue #11555 is the open admission that per-tenant schemas need a *second,
bespoke* migration engine; #19863/#12936/#13189 are upgrade cascade-failures
("column already exists", crash-restart loops) following from non-atomic
per-tenant DDL. Cybertec's *Too many tables are bad for you* documents the
Postgres-side pathology (catalog joins degrading faster than linearly,
`pg_attribute` bloat, autovacuum stalls).

**Rejected: pure EAV (Attio) and side-table-per-module (SuiteCRM).** Attio's
bitemporal typed value store works because they built a bespoke query engine;
Salesforce's answer to the same problem was writing a custom query optimizer.
JSONB benchmarks against EAV show orders-of-magnitude wins, widened further by
GIN + `@>`.

Three amendments the research forces onto the JSONB choice:

1. **The registry must support index promotion.** GIN indexes containment and
   equality, *not* range — `custom->>'renewal' > '2026-01-01'` will not use one.
   Marking a field indexed emits an expression index or generated column. This
   is the main thing the registry buys beyond documentation.
2. **High-churn derived data never goes in `custom`** — JSONB updates rewrite
   the whole value under a full row lock. Relationship scores, last-interaction
   timestamps and AI outputs get real columns.
3. **`custom` is for user-defined data only.** The moment a product feature
   filters, sorts or joins on something, it earns a column.

**Tables that do the real work:**

- **`interactions`** — `(id, org_id, account_id, owner_user_id, occurred_at,
  kind, direction, thread_id, from_channel, to_channels[], subject_hash, body,
  external_id, source)`. The primitive. Body is retained (lesson 3 above) but
  **body access is scoped to `owner_user_id`** while every other column is
  org-wide — see the contacts section. `account_id` records which connected
  mailbox observed it, so the same message arriving in two users' mailboxes
  deduplicates on `external_id` per account without collapsing provenance.
- **`contact_channels`** — `(person_id, kind, value_normalized, value_raw,
  is_primary)`, kind ∈ email/phone/signal/telegram/handle, unique on
  `(kind, value_normalized)`. Omnichannel identity resolution: an inbound Signal
  message, a Telegram chat and an email thread all resolve to one person.
- **`associations`** — `(from_type, from_id, to_type, to_id, role, attributes
  jsonb, valid_from, valid_to)`, HubSpot's shape: many-to-many by default,
  surfaced bidirectionally with zero configuration. **Relations are never
  modelled in JSONB.** Roles are a closed vocabulary declared per module and
  validated on write — free text drifts (`works_at` / `works at` /
  `employee_of`) and silently stops matching.

  The date range is what makes history answerable: *was* an LP in Fund I but not
  Fund II, *was* CFO until March, *sat* on the board during the investment. It
  also collapses what would otherwise be four separate tables — employment,
  board seats, co-investment, LP relationships — into one (R1 applied to the
  domain rather than to providers). See `docs/VERTICAL-ASSET-MANAGEMENT.md`.
- **`metric_facts`** — `(subject_type, subject_id, period_start, period_end,
  metric_key, value_numeric, value_text, currency, source, confidence,
  document_id)`. Event-shaped and period-shaped data are different tables.
  "Acme's Q3 EBITDA" is not an event, so it cannot live in `interactions`; it is
  not a property of the company, because next quarter there is another one; and
  it cannot live in `custom` without an expression index per metric. Retrofitting
  this is the same class of pain as retrofitting the interaction log, so it ships
  alongside it in M2.
- **`documents`** — `(subject_type, subject_id, kind, filename, storage_key,
  mime, bytes, uploaded_by, valid_from, valid_until, status)`. `valid_until`
  is load-bearing: an expired compliance document must **gate an action**, not
  render a warning — the same "gates raise, never no-op" rule applied to
  compliance.
- **`field_provenance`** + append-only **`field_history`** — source ∈
  `human | sync:graph | sync:google | ai:<agent> | enrichment:<vendor>`, with
  confidence, model and prompt version. **Precedence is enforced: human >
  provider sync > AI.** An AI write to a human-owned field goes to the queue.
- **`proposed_changes`** — `(record_ref, field_key, current_value,
  proposed_value, rationale, citations[], agent, confidence, status,
  reviewed_by, reviewed_at, decided_by)`.
- **`trusted_senders`** + `persons.auto_accept` — see below.
- **`embeddings`** — `(owner_type, owner_id, visibility_user_id, chunk_kind,
  content_hash, model, dim, embedding vector)` with HNSW, keyed by content hash
  so re-embedding is idempotent and a model change is a backfill. **Vectors
  never go on record tables.** The interaction log is the RAG corpus, so an
  embedding derived from a message body inherits that body's owner scope —
  `visibility_user_id` is null for org-wide content and set for body-derived
  content, and retrieval filters on it. Without this, semantic search silently
  becomes a channel for reading other people's mail.

Plus CATO's `work_queue` + `workers`, and `llm_calls` for audit.

### Auto-accept — ported from Cal, generalized

Cal's rule is the right shape and transfers directly to any CRM automation:

- **Trust is defined in exactly one function** over two sources — people
  (`persons.auto_accept`) and services (`trusted_senders` patterns, which may be
  a full address or a whole domain). A service is *not* a person with a name and
  role, and domain patterns can't be faked into a contacts directory, so they
  stay separate tables behind one lookup.
- **`trust_reason()` returns a human-readable reason** (`contact:Sam Rivera`,
  `sender:@school.edu`) written to the audit trail, not just a boolean.
- **Trusting a public mail domain is refused unless explicitly confirmed** —
  `@gmail.com` would auto-accept from anyone on the internet. Cal ships the
  blocklist; port it.
- **Both halves are required**: a trusted sender in an unlisted category still
  waits, and an unlisted sender in a listed category still waits.
- **Categories are a closed vocabulary**, validated against the enum at the same
  layer as other field checks so an unknown value triggers the repair-retry
  rather than reaching a real calendar. Free text drifts
  ("kids sports"/"sport"/"Sports") and silently stops matching. **A category the
  model cannot determine stays null and never matches — unclassifiable input
  fails safe to the approval queue.**
- **Evaluated only after the approval mode declines to act**, so it can accept
  something that would otherwise sit pending but can never hold back what a mode
  already approved. The master write gate overrides everything.
- **The decision records why**: `auto:mode` vs `auto:rule(category,trust)`.

### Multi-user and permissions

EspoCRM's model, the right size for a small team: role → object → CRUD grid,
record visibility **all / team / own / none**, multiple roles merging
*permissively*, field-level masking for sensitive fields, and a hard
admin/non-admin split. Explicitly skipped: sharing rules, territory hierarchies,
criteria-based sharing, manual record shares — machinery for 5,000-seat orgs and
the most-regretted complexity in CRM implementations.

**Enforcement is split, deliberately:**

- **Postgres RLS for the `org_id` boundary only.** Measured overhead 1–6%,
  sub-millisecond policy evaluation at 50M rows, `SET LOCAL` <0.1ms. It makes
  cross-tenant leakage structurally impossible — including from `psql`, cron
  jobs and AI tooling, exactly the paths that bypass app-layer checks. Required
  discipline: `FORCE ROW LEVEL SECURITY` (the table owner otherwise sees
  everything), composite indexes with `org_id` **leading** (missing this is two
  orders of magnitude slower), no `BYPASSRLS`/superuser app connections, and
  `SET LOCAL` *inside* the transaction because the pooler runs in transaction
  mode.
- **Record visibility (own/team) as predicates injected at one choke point** in
  `core/repository.py` — these change per query and are easier to index and
  debug as SQL you can read.

Auth upgrades Cal's single-operator HTTP Basic to real sessions, but **ports
`dashboard_auth.py`'s hashing wholesale**: stdlib `pbkdf2_hmac` +
`compare_digest`, the self-describing `pbkdf2_sha256$<iterations>$<salt>$<hash>`
format so iterations can be raised without invalidating existing hashes, both
credential halves always evaluated so timing doesn't leak which was wrong, and a
malformed hash denying access rather than 500-ing every request.

Cal's **fail-open-when-unconfigured** choice does *not* transfer — it exists so
pulling the change can't lock a single operator out of their own Settings page.
A multi-user CRM fails closed, with a first-run setup path instead.

#### What "EspoCRM-sized" excludes, and why

The permission model is four things: a role → object → CRUD grid, record
visibility at all/team/own/none per object per role, field masking on a small
set of sensitive fields, and an admin/non-admin split for settings and provider
credentials. Multiple roles merge permissively. That is one predicate injected
at one choke point.

Enterprise permissions add criteria-based **sharing rules**, **territory
hierarchies** where visibility is inherited through a management tree, org-wide
defaults with per-object overrides, manual per-record shares, and a general ABAC
policy engine. Salesforce's version has seven interacting mechanisms deciding
visibility on a single row.

The cost is not the feature count. It is that visibility stops being a predicate
that can be read and becomes a **computed set**: enterprise implementations
maintain materialized share tables and recalculate them on ownership change,
role moves and rule edits, which is a documented multi-hour operation on a large
org. Every query path, export, background job and AI agent must route through
that engine or it leaks.

For a team where everyone can see everything and `owner_id` mostly answers "who
is on point," the four items above are the entire requirement. Because
visibility lives in one function, adding a sharing rule later is a change to
that function, not a data migration.

### Contacts: typed in *and* observed, across many mailboxes

Both creation paths are first-class. A person record carries `source`
(`human | derived | sync:<provider>`) and `is_derived`; typing one in creates it
directly, and a derived record is promoted the moment a human edits it. Neither
path is a special case of the other.

**Multiple mailboxes in one org feed one contact graph.** If two users have
connected accounts, mail from `bob@acme.com` in either resolves through
`contact_channels` to the *same* person record, and that person's timeline
carries interactions from both. This is the point — "who here already knows
Bob?" is not answerable otherwise, and it is why the interaction log is org-
scoped rather than user-scoped.

That creates one privacy decision that must be explicit, because the default
falls out either way and only one of them is defensible:

> **Interaction metadata is org-wide; message bodies are private to the mailbox
> owner by default.**

Who corresponded with whom, when, and how often is what powers relationship
strength, staleness and warm-intro paths — it must be shared or the graph is
worthless. The message text is a different thing, and one user's correspondence
should not become readable to the whole org as a side effect of a contact
appearing in both mailboxes. This is Affinity's model.

Consequences to enforce:

- `interactions` splits visibility: metadata columns are readable org-wide,
  `body` is gated to the owning account's user (and admins, explicitly).
- LLM enrichment over **metadata** may run org-wide; extraction over **bodies**
  runs only within the owning user's scope, and any derived field written from a
  body records that scope in `field_provenance`.
- A body-scoped embedding must never be retrieved into another user's RAG
  context. `embeddings` therefore carries the same owner scope as its source.

### Modularity

EspoCRM's shape, modernized — the only surveyed system with genuine modularity
at a cheap seam. A module is a directory with a manifest declaring entities,
routes, jobs, event subscribers, UI slots and permission scopes; metadata is
recursively deep-merged over core.

Rejected: Odoo's in-place model monkey-patching (heavily customized migrations
run ~3x longer), Erxes' GraphQL federation + micro-frontends (distributed-systems
tax before there are users), and Twenty's configurable-but-not-extensible model
(extending behavior means forking). **Start declarative**; add a narrow,
versioned server-side hook API only when a real module needs it.

### LLM layer

Cal's task-agnostic split: `llm/router.py` exposes `call_llm(prompt, *,
want_json, temperature, max_tokens)`, `extract_structured(prompt, schema, *,
validate)`, `list_models(provider)`, `check_llm_status()`,
`test_llm_connection(overrides)`; each task owns its prompt + schema +
validation in `extraction/`.

**Port Cal's provider fallback chain**, which is the most important recent
addition and depends on a distinction worth stating explicitly:

- **`ProviderUnavailable`** — unreachable host, missing key, 401/403/429, 5xx —
  is the *only* thing that cascades to the next provider in the chain.
- **Malformed output** gets its one repair-retry and then fails the item to
  `needs_review`. Cascading on it "would burn a round-trip through every
  provider on every ambiguous" input.
- `extract_structured` is therefore **a chain loop wrapping an inner
  per-provider repair** — self-recursion would restart the whole cascade on a
  bad response.
- An empty chain falls back to the single active-provider setting, so existing
  config and the Settings UI keep working.
- Config round-trips through env, so a list must be **JSON-encoded** — `str()`
  on a list produces a Python repr that cannot be parsed back.

Also port: **wake-on-LAN always blocks** rather than skipping to the next
provider (skipping "defeats the point of running a local box"), bounded by a
failure cooldown so one failed wake doesn't cost a full timeout per message.
Where Cal and JA diverge elsewhere, take JA's newer work: the shared
`_call_openai_compatible()` path (one call shape for OpenAI, vLLM, LM Studio),
strict-mode JSON Schema, and `_prime_ollama()` for `num_ctx`/`keep_alive`.

AI features follow the pattern that actually landed in 2025–26: **AI attributes**
that auto-fill via research/classification/summarization, writing through
`proposed_changes` — suggestion, not autonomy.

### Messaging control loop — ported from JA

JA's `messaging.py` provider registry generalizes directly:
`send(text, thread_to=)` / `receive_commands() -> [(text, quoted_id)]`, opaque
message ids the caller stores and never interprets. Telegram is a second entry
in `_PROVIDERS`.

`reply_loop.py`'s safety rules are **requirements**, each found the painful way:

- **No LLM anywhere in the action path.** Exact-match vocabulary only — a real
  write "can only ever be triggered by exact-match code, never by anything
  resembling judgment."
- **Quote-targeting, and a quote that does not resolve stops cold** — it must
  *not* fall back to most-recent-pending. JA found live that a conversational
  reply to a non-actionable notification was redirected into an unrelated stale
  item and sent as a real email.
- **Compare-and-swap claim before acting**, reverting to the *original* status
  on failure so the item stays retryable. Without it, a crash between send and
  commit causes a duplicate send.
- **Failures reach the user on the channel**, never vanishing into a daemon's
  catch-all.
- **`signal-cli` temp-dir cleanup** — 0.14.5 extracts a ~167MB `libsignal*` temp
  dir per invocation and never cleans up; JA lost Signal entirely to an
  exhausted `/tmp` quota.

One structural change: JA gates every command on a single owner phone number.
Here the sender resolves through `contact_channels` → `users`; an unrecognized
sender is ignored exactly as JA ignores a non-owner.

## Decisions

All four are ratified.

1. **Stack — FastAPI + React/TypeScript/Vite.** Confirmed; the same shape is
   already working in sibling projects. Python keeps direct line-of-sight to
   Cal's and JA's routers, extraction, Graph and signal-cli code, all of which
   would otherwise be rewritten — violating R1 before the repo exists.
   Dependencies stay minimal.
2. **Multi-user — EspoCRM-sized**, single seeded org, RLS on `org_id`. See
   "What EspoCRM-sized excludes, and why" above for the boundary and its
   rationale. Tightening later is a change to one function, not a migration.
3. **Sync — one-way first** (provider → CRM with tombstones), write-back later
   per-field through the approval queue.
4. **Contacts — typed in *and* observed**, over an org-wide interaction log
   spanning every connected mailbox, with **metadata org-wide and message bodies
   private to the mailbox owner**. Both creation paths are first-class; derived
   records promote on human edit. See "Contacts: typed in *and* observed" above.
5. **Primary vertical — asset management**, built as a module over a
   domain-neutral core, with flexibility beyond it preserved by the two-tier
   model. The governing rule is that **"investor" is not an entity type but a
   dated role** an organization plays relative to a fund, because in this
   business one legal entity is routinely an LP, a co-investor and a
   counterparty at the same time. Core must never mention a fund. See
   `docs/VERTICAL-ASSET-MANAGEMENT.md`.

The interaction log remains the expensive one to change: retrofitting it under
an existing contacts model is the hard version, and the metadata/body visibility
split has to be built into the schema rather than added as a filter later.

## Phases

| Phase | Delivers | Done when |
|---|---|---|
| M0 | Scaffold, `CLAUDE.md` + R1–R5, `.claude/agents/`, `tokens.css`, `config.py`, pool + schema, users/roles/sessions/RLS | `psql` shows the schema with `FORCE RLS`; login works; first-run setup path exists |
| M1 | Registry + generic repository + event bus + REST + table/detail/saved views + dated `associations` | CRUD on all core entities through one code path; one org holds four roles at once |
| M2 | `interactions` + person/org derivation + `contact_channels` + merged timeline + `metric_facts` + `documents` | Importing a mailbox materializes people nobody typed; a quarterly KPI charts |
| M3 | LLM router + fallback chain + settings UI | Chain rolls down on `ProviderUnavailable`, does *not* on malformed output |
| M4 | OAuth + Microsoft & Google contact sync + provenance | Delta advances; a forced 410 recovers without data loss |
| M5 | Calendar + scheduling extraction + `proposed_changes` + trust/categories | A proposed meeting queues; auto-accept fires only on category **and** trust |
| M6 | Signal + Telegram command loop | Approve from a phone; both channels resolve to the right user |
| M7 | Work queue + workers, deal rotting, Cmd-K, pgvector search, hardening | Full sync runs as a background job |
| M8 | `modules/funds`: funds, commitments, capital transactions, positions, investments | "Who committed to Fund II, what have they paid, what is it worth" — with core still not mentioning a fund |
| M9a | Investor profiles, versioned questionnaires, derived mandates | An investor can be classified by what they want to invest in, and corrected by hand |
| M9b | Two-way matching, offerings, human-granted sight of them | "Which investors fit this offering" answers, and nothing is granted automatically |
| M9c | External identity class, gated investor portal, public site | An investor sees only their granted offerings; a logged-out visitor sees none |

M9a is worth pulling forward: classifying existing investors is useful to the
internal CRM on its own, and its data is what makes the matching in M9b worth
building. See `docs/INVESTOR-PORTAL.md`.

## Verification

- **Tests:** pytest with Cal's conftest isolation and mocked HTTP; a
  transactional-rollback fixture per test against a throwaway Postgres. Cal's
  suite is at 288 — treat that density as the bar.
- **Schema:** verified with `psql` against the live database — never trust the
  spec (CATO's rule 1).
- **RLS:** connect as a second org and assert zero rows; `EXPLAIN`-assert
  `org_id` leads the index on every hot table.
- **Config:** a regression test that saving one settings section does not blank
  another, and that clearable keys can actually be blanked.
- **Gates:** a test that the approve path refuses to write when the master gate
  is off, and *raises* rather than silently no-oping.
- **Extraction validation:** explicit cases for `duration_minutes="90"`,
  `attendees` as a bare string, and `"2026-13-45"` — each must trigger the
  repair-retry, not reach a provider.
- **Timezone:** a DST-transition day must produce a 23h and a 25h window; and
  **a live smoke test that a created event lands at the right wall-clock time**
  before any unattended calendar write is enabled.
- **Sync:** fixture Microsoft + Google accounts; assert delta tokens advance,
  **force a 410 and assert clean full-resync with tombstone diffing**, assert
  dedupe on normalized channel value and provenance rows written.
- **Channels:** JA's `--once` mode drives one command per run; live smoke test
  per channel before enabling write gates.
- **Live-action gates:** every destructive path stays off behind an explicit
  setting, defaulting to `manual` approval — Cal's discipline, unchanged.
