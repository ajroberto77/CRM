# Architecture — Load-Bearing Mechanisms

This is the part of the codebase that makes "one code base, similar things
never developed twice" (`CLAUDE.md`'s stated goal) actually hold, rather than
just being an aspiration. Everything here was read directly from source in
this session (not summarized from docs), and cross-checked against the test
suite (788 tests passing at time of writing).

## 1. The entity registry — `server/core/registry.py`

The single source of truth for "what an entity is." A registered `EntitySpec`
drives the generic repository (CRUD), the generic REST router, and the
generic list/detail UI — a new entity is a registry entry, never a new
router/repository/component stack (R4).

- **`FieldSpec`** — one field: `kind` (a closed vocabulary: text, number,
  date, datetime, boolean, select, multiselect, currency, url, email, phone,
  uuid, jsonb), `column` xor `custom_key` (exactly one), `filterable`/
  `sortable`/`writable`, `required`, `options`, `label`, `references`/
  `references_type_field` (fixed or polymorphic FK target), `write_level`/
  `read_level` (permission gating below the entity-wide level), `compute`
  (pure function of `(record, context) -> value` for a derived field, never
  a stored/recomputed column), `normalize` (pure function of submitted value
  -> canonical form, applied at write time), `show_in_detail` (whether the
  generic field-list UI shows this field at all — added for a compute field
  that already has its own dedicated presentation, so it isn't duplicated as
  a raw value).
- **`EntitySpec`** — one entity: `name`, `table`, `fields`, `label_field`,
  `default_sort`, `searchable`, `supports_custom_fields`, `admin_only`,
  `module` (which module registered it — `"core"` or a module name),
  `context_builder`/`context_builder_ids` (per-call or per-row-batch context
  built once and handed to every field's `compute()`), `nav`/`nav_group`/
  `nav_order` (sidebar placement), `list_columns` (curated default table
  columns).
- **`AssociationRole`** — one relationship type: `name`, `from_types`,
  `to_types`, `inverse_label`, `symmetric` (canonicalized on write so `A rel
  B` and `B rel A` can't both exist), `hierarchical` (forms a rollup
  dimension walked by `server/core/hierarchy.py`'s cycle-checked recursive
  CTE), `module`, `label`/`group`/`group_order` (presentation).
- **`ProfileBlock`**/**`DashboardTile`** — role-gated record-page panels and
  per-vertical dashboard tiles, both module-contributed, both consumed
  generically by the frontend (added this session, Phase 12).
- **`register()`/`register_role()`/`register_profile_block()`/
  `register_dashboard_tile()`** — idempotent (re-declaring the same name on a
  second `install()` — every real app startup and every fresh `TestClient`
  both call `install_enabled_modules()` — does not duplicate it), and cross-
  module name collision (two modules registering the same entity/role name)
  raises `RegistryError`.
- **`register_validator()`** — a write-time rule that runs synchronously
  inside `repository.create()`/`update()`/`delete()`'s own transaction, after
  the row is written but before commit, and can raise to abort the write.
  This is the seam that lets `modules/investor_portal` enforce "a commitment
  cannot close without an executed subscription agreement" on a
  `modules/funds` entity without either module knowing about the other.
- **`register_org_seed()`** — a callable invoked once with a new org's id,
  right after `create_org()`'s transaction commits, for a module's default
  reference rows (investor categories, GP roles, default saved views).
- **`verify(tables)`** — runs at app startup (and in tests), checks every
  registration against the real schema: missing table/column, unknown field
  kind, a computed field left filterable/sortable (would silently emit wrong
  SQL), a reference to an unknown entity, a bad `nav` value, a `ProfileBlock`
  targeting an unknown entity/role/field, a `DashboardTile` grouping/summing
  by a nonexistent field. An empty list is the only acceptable result in a
  deployed system.

## 2. The generic repository — `server/core/repository.py`

The one place every read and write of every registered entity goes through.
No other file opens a cursor against a record table.

| Operation | Applied, in order |
|---|---|
| `list`/`get`/`count` | `require('read')` → read visibility predicate in WHERE → `_finalize_read()` |
| `create` | `require('create')` → `reject_masked_writes()` → owner forced → `_finalize_read()` |
| `update` | `require('edit')` → `reject_masked_writes()` → **edit** predicate in the UPDATE (re-checked, not the read predicate — a user may see a row but not edit it) → `_finalize_read()` |
| `delete` | `require('delete')` → **delete** predicate in the DELETE |

`_finalize_read()` applies role-level field masking (`mask_record()`), then a
second orthogonal pass for `FieldSpec.read_level` (row-owner field scoping —
e.g. `interaction.body` is visible only to the row's own owner regardless of
the principal's overall read level, safety rule 10), then every field's
`compute()`.

A row the principal cannot see is `NotFound`, never `PermissionDenied` — a 403
on an invisible row would confirm it exists, exactly what the visibility
level was meant to hide. A row they can see but not edit is
`PermissionDenied`, because that isn't a secret.

Optimistic concurrency: `update(..., if_unmodified_since=...)` compares
against the row's own `updated_at`; a mismatch raises `Conflict`. This is what
`core/proposals.py`'s approve/decline compare-and-swap and inline-editing's
race protection both build on.

## 3. The permission model — `server/core/permissions.py`

An EspoCRM-shaped model, deliberately not more: role → object → action grid
(create/read/edit/delete), record visibility per action (`all`/`team`/
`own`/`none`), field masking (hide or read-only, per role), and a hard
admin/non-admin split. No sharing rules, no territory hierarchies, no ABAC
engine — the stated reason is that those turn visibility from a predicate you
can read into a computed set backed by materialized share tables, which every
query path, export, background job and AI agent would then have to route
through or leak.

- **`Principal`** — built once per request, carries merged `ObjectPermissions`
  per object. An admin short-circuits to full access everywhere without
  querying role scopes.
- **`visibility_predicate()`** — the SQL fragment for `own`/`team`/`all`/
  `none`, deliberately excluding the org boundary (RLS already enforces that;
  duplicating it here would be a second implementation of the same rule).
- **`require_field_readable()`** — the gate that makes filtering/sorting on a
  masked field impossible: without it, `salary > 200000` would return exactly
  the right rows even though `salary` never appears in the response, making
  the value recoverable by binary search. The same reasoning was applied this
  session to the new `has_role` filter clause (see §7): an `EXISTS` against
  `core.associations` doesn't join in the other side's row, so it separately
  checks read access on the far side's entity type before compiling.
- **`system_principal(org_id, reason)`** — the principal used by event
  subscribers, org seeds, and background jobs: full access within the org,
  but distinguishable from a human actor in the audit trail (`is_system`,
  `system_reason`), and every event it causes is recorded as system-originated.

## 4. Multi-tenancy — Postgres Row-Level Security + `server/db/pool.py`

RLS carries the org boundary and nothing else — absolute, cheap, impossible
for any code path to bypass. The permission model above carries record
visibility (own/team), because those predicates change per query and are far
easier to index and debug as SQL than as RLS policies.

- **One connection pool** (`psycopg2.pool.ThreadedConnectionPool`), a
  singleton — no second pool anywhere, including the DDL path (which borrows
  from the same pool, just switches to autocommit for `CREATE INDEX
  CONCURRENTLY`).
- **`transaction(org_id, user_id, readonly=False)`** is the *only* way to get
  a cursor. Tenant context is set via `SELECT set_config('app.org_id', ...,
  true)` — **`SET LOCAL`, never plain `SET`** — because the app may run
  through a transaction-mode pooler (pgbouncer), where a plain `SET` persists
  on the physical connection after the transaction ends and leaks tenant
  context to the next borrower. This is treated as severe enough that
  `SET LOCAL` is used unconditionally, pooler or not.
- **`system_transaction()`** — explicitly no tenant context, for schema
  migration and first-run bootstrap only. Not a privilege escalation (it sets
  no GUC, so under RLS it sees no tenant rows) — it exists to make
  context-free access greppable rather than looking like a bug.
- **`healthcheck()`** reports whether the app's database role is dangerously
  privileged (`BYPASSRLS` or superuser would silently defeat every policy) —
  surfaced at `/health`, not assumed safe.
- Every table carries `org_id`; every composite index puts it first (a
  missing leading column is "two orders of magnitude slower, not a rounding
  error," per `CLAUDE.md`).

## 5. The association model — `server/core/associations.py`

The mechanism that makes a fixed-schema `organizations`/`persons` pair express
an unbounded, evolving relationship graph without ever forking those tables
per vertical. This is *the* answer to "how does this stay domain-neutral
while supporting a whole investing vertical" (R6).

- **One row, read from both ends.** A relationship is stored once
  (`core.associations`: `from_type`, `from_id`, `to_type`, `to_id`, `role`,
  `attributes` jsonb, `valid_from`/`valid_to`). `core.association_edges` is a
  security-invoker view unioning the table with itself (excluding self-loops
  on the second branch) so either endpoint can query without knowing which
  side it's on.
- **Direction is presentation, not storage.** `role_presentation()` derives
  the human label for each direction from `AssociationRole.label`
  (outbound)/`inverse_label` (inbound) — this is the one place both the
  "+ Link" role picker and the related-records panel derive a role's label,
  so it can't drift between the two.
- **Associations are not a permission subject.** No `owner_id`; access is
  derived — readable when both endpoints are, writable when the principal can
  edit the near side and read the far side.
- **Batched hydration, not a join.** `related_blocks()` fetches every edge in
  one query, then hydrates the far side via `repository.fetch_many()` grouped
  by entity type — O(entity types) queries, not O(related rows) — and a
  target the principal can't read is simply absent, never joined in behind
  the repository's back.
- **Symmetric roles are canonicalized on write** (endpoints sorted) so `A
  co_investor_in B` and `B co_investor_in A` can't both exist and double-count
  the relationship.
- **Hierarchical roles** (`owned_by`, `rolls_up_to`) form a rollup dimension
  walked by `server/core/hierarchy.py`'s recursive CTE, cycle-checked at write
  time by `associate()` before the edge is inserted.
- **`role_summary_for()`** (added this session, Phase 9) — a record's live,
  emergent "type": every role it currently plays, batched for a whole page of
  rows in one query via `EntitySpec.context_builder_ids`. This is the
  mechanism that lets a record page show "LP in / Portfolio of / Evaluating"
  pills without ever persisting a type column — "a role is not an entity
  type" (R6) is enforced by construction, not just convention.
- **The generic `has_role` filter** (added this session, Phase 11) — a
  `compile_filter()` clause with no `field`, compiling to an `EXISTS` against
  `core.associations`, so a saved view can select "organizations that are
  portfolio companies" the same way it selects "organizations named Acme."

## 6. The event bus — `server/core/events.py`

The single extension seam. A transactional outbox, not a naive callback list:
the event row is written **inside** the repository's own write transaction
(so a record write and the event describing it commit or roll back
together), and subscribers run **after** that commit.

- Subscribers do not run inside the write's transaction — they'd hold a row
  lock and a pooled connection for their whole duration (the first subscriber
  makes an HTTP call to Signal; at four seconds each, ten concurrent saves
  would exhaust the default 10-connection pool and deadlock the app), and any
  module's exception would otherwise roll back a core CRUD write.
- **A throwing subscriber never fails the request.** It's caught, logged, and
  recorded on the event row (`delivery_state='failed'`, `last_error`) — the
  write already committed, so re-raising would return 500 for a write that
  succeeded and invite a client retry that duplicates it.
- **Subscribers are not a privileged path**: `RecordEvent` carries no cursor
  (a subscriber physically cannot write inside the writer's transaction), and
  a subscriber that writes goes back through the ordinary `repository` with a
  `system_principal`.
- **Delivery is in-process, at-most-once** on the synchronous happy path —
  fine for cache invalidation, not fine for "send a message." The rows are
  durable, so `server/jobs/event_redelivery.py`'s sweep (part of the M7 work
  queue) drains `delivery_state IN ('pending', 'failed')` rows older than a
  grace period for at-least-once delivery, without any subscriber changing —
  the subscriber signature is the contract, the delivery mechanism is not.
- A recursion depth cap (`MAX_DEPTH = 4`, a `ContextVar`) stops a subscriber
  that updates its own subject from recursing the process to death.

## 7. The module system — `server/core/modules.py`

`config.get_enabled_modules()` (env-configured, comma-separated, default
`"funds,investor_portal"`) names which `modules/<name>` packages get
installed — **by directory name only**, resolved dynamically via
`importlib.import_module(f"modules.{name}")`. No literal `import
modules.funds` anywhere under `server/` — enforced by a tokenized scan in
`tests/test_vertical_funds.py` that walks every file under `server/` looking
for vertical vocabulary (`fund`, `commitment`, `lp_in`, `mandate`,
`questionnaire`, etc.) in any identifier, attribute, or string literal
(comments/docstrings are exempted, since explaining *why* a module exists is
allowed prose).

`install_enabled_modules()` registers core first (entities, then a handful of
core-but-not-generic behaviors — `derivation.py`, `scheduling_pipeline.py`,
`deal_activity.py`, `interaction_embeddings.py` — each wired the same way a
module would be, just not gated behind the enabled-modules list since they're
domain-neutral), then lets each enabled module's own `install()` register its
entities/roles/validators/tables/seeds/profile-blocks/dashboard-tiles, in that
order (a module's roles can reference core entities, never the reverse).

The frontend mirrors this exactly: `web/src/modules/index.ts` uses Vite's
`import.meta.glob('./*/index.ts', { eager: true })` so a module's frontend
bundle self-registers (profile block components, etc.) with no module name
ever spelled in `main.tsx` or any other core frontend file — added this
session specifically to close a gap a code-review agent found (an earlier
draft had `main.tsx` hardcode `import './modules/funds'`).

## 8. The six provider-dispatch axes (R3)

| Axis | Dispatcher | Adapters implemented |
|---|---|---|
| LLM | `server/llm/router.py` | ollama, openai, anthropic, gemini, claudecode — all 5 implemented |
| Mail | `server/providers/mail.py` | **not implemented** — no dispatcher file, no adapters exist |
| Calendar | `server/providers/calendar.py` | microsoft, google — both implemented |
| Contacts | `server/providers/contacts.py` | microsoft, google — both implemented |
| Messaging | `server/channels/dispatch.py` | signal, telegram implemented; email **not implemented** |
| E-signature | `server/providers/esign.py` | docusign, dropboxsign, pandadoc, adobesign — all 4 implemented (never live-tested against real vendor sandboxes) |

No other file may branch on provider identity within an axis — each
dispatcher is the one `if/elif` (or equivalent) over that axis's provider
name. Adding a provider is one new `<provider>_*.py` file plus one branch in
its dispatcher.

**Two concrete gaps worth flagging prominently** (both detailed further in
`05-messaging-channels.md` and `06-sync-and-jobs.md`): the Mail axis doesn't
exist as code at all, and there is currently no automated path from an inbound
email/Signal/Telegram message into a `core.interactions` row. The scheduling
pipeline, semantic search, and contact-derivation machinery are all fully
built to consume interactions the moment a producer exists — this is the
single most natural integration point for a sibling project whose strength is
data collection.

## 9. Custom fields, saved views, and other platform-level entities

Saved views (`core.saved_views`) and custom field definitions
(`core.custom_fields`) are registered entities too, not bespoke routers — R4's
"a new entity is a registry entry" applies to the platform's own
configuration objects as much as to a business entity. A saved view's
`filters` JSON is recompiled and re-permission-checked by the executing user
on every execution — never cached as SQL, so a view a user could see when it
was created but can no longer read some part of degrades safely rather than
leaking a stale, wider-privileged query plan.

## 10. What's new this session (Phases 9–12)

For context on what's freshly built vs. long-standing: this session added (a)
`role_summary` — the batched, computed "live role graph" field described in
§5; (b) `is_public` on organizations plus a `core.securities` table
(ticker/exchange) and an `evaluating` role in `modules/funds`, distinguishing
public/private and prospective/actual portfolio companies; (c) the generic
`has_role` filter primitive plus nine seeded default saved views and a sidebar
nav restructure nesting saved views under each object; (d) role-gated profile
blocks on record pages and a registry-driven per-vertical dashboard at
`/dashboard/:navGroup`. All of it went through this repo's own review
discipline (`no-dupes`, `schema-guardian`, `ui-reviewer` agents) before
merging, and is covered by new tests in `tests/test_role_summary.py`,
`tests/test_public_targets.py`, `tests/test_role_filter_and_seeded_views.py`,
and `tests/test_profile_blocks_and_dashboard.py`.
