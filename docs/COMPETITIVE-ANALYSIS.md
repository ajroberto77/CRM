# Competitive analysis — CRM platforms

Evidence base for the decisions in `docs/DESIGN.md`. Surveyed: Twenty, EspoCRM,
SuiteCRM, Odoo, Monica, Erxes, Atomic CRM (open-source/self-hosted); Attio,
Salesforce, HubSpot, Pipedrive, Affinity, Clay (commercial, for data-model and
UX reference).

## 1. Data model

| System | Mechanism | Cost |
|---|---|---|
| Salesforce | Metadata layer + universal data dictionary + pivot tables (`MT_Data`, `MT_Indexes`), no runtime DDL, partitioned by OrgID | Required building a **custom query optimizer** because the RDBMS planner cannot reason about virtualized schema |
| SuiteCRM / Sugar | Fixed core tables + **side table per module** (`contacts_cstm`, `_c` suffixes) driven by PHP `vardefs` | Every custom field is a join; schema changes need "Quick Repair and Rebuild" to emit DDL |
| Twenty | **Real DDL at runtime** — one Postgres schema per workspace, custom objects become real tables | See below |
| Attio | Strongly-typed attribute-value store with **bitemporal values** | See below |
| EspoCRM | JSON metadata (`entityDefs`) → real columns via a rebuild step | Middle ground |
| Monica / Atomic CRM | Plain fixed tables | No runtime extensibility |

### Twenty — confirmed, and instructive

The schema-naming function is literally `` `workspace_${uuidToBase36(workspaceId)}` ``
(`packages/twenty-server/src/engine/workspace-datasource/utils/get-workspace-schema-name.util.ts`),
with schemas created and dropped at provisioning time. The costs are observable
in their own tracker, not theoretical:

- **Migrations bifurcate.** [#11555](https://github.com/twentyhq/twenty/issues/11555)
  is the open admission that per-tenant schemas need a *second, bespoke*
  migration engine. They ship per-version "upgrade commands" that iterate every
  workspace.
- **Upgrades cascade-fail.** [#19863](https://github.com/twentyhq/twenty/issues/19863)
  (v1.21→v1.23 "column already exists", crash-restart loop),
  [#12936](https://github.com/twentyhq/twenty/issues/12936),
  [#13189](https://github.com/twentyhq/twenty/issues/13189),
  [#20699](https://github.com/twentyhq/twenty/issues/20699). Per-tenant DDL makes
  upgrades non-atomic, so partially-applied states are normal.
- **Catalog cost.** Cybertec's [*Too many tables are bad for you*](https://www.cybertec-postgresql.com/en/too-many-tables-are-bad/)
  is definitive: pain grows linearly with table count, catalog joins degrade
  *faster* than linearly, `pg_attribute` bloat has been reported at 200GB with
  new connections stalling in startup, and autovacuum spends minutes just
  deciding what to vacuum. The conclusion is that schema-per-tenant is viable
  only with few tenants; beyond that you need separate databases or "a single,
  perhaps partitioned, table with row-level security."

### Attio

Objects ≈ tables, records ≈ rows, attributes ≈ columns, over a fixed type system
(`text, number, select, multiselect, status, date, timestamp, checkbox,
currency, record-reference, actor-reference, location, domain, email-address,
phone-number, interaction`). The tell that this is *not* columns-per-attribute is
**historic values**: every value carries `active_from` / `active_until` /
`created_by_actor`, queryable with `?show_historic=true`. That is a value-row
store with validity intervals — EAV done properly, with types first-class and
provenance on every value, at >250,000 events/min peak.

### Decision

**Fixed core tables + JSONB `custom` + a `custom_fields` registry**, with a
second tier (`records(object_id, org_id, data jsonb)`) for user-defined objects.

For: JSONB beats EAV by orders of magnitude in
[published benchmarks](https://coussej.github.io/2016/01/14/Replacing-EAV-with-JSONB-in-PostgreSQL/),
GIN + `@>` containment widens the gap further, and you get one migration path
and a planner that can see your data. Twenty's entire upgrade-pain surface
disappears.

Three amendments this forces:

1. **The registry must support index promotion.** GIN indexes containment and
   equality, **not range** — `custom->>'renewal' > '2026-01-01'` will not use
   one. Marking a field indexed must emit an expression index or generated
   column. This is the main thing the registry buys beyond documentation.
2. **High-churn derived data stays out of `custom`.** JSONB updates rewrite the
   whole value under a full row lock. Relationship scores, last-interaction
   timestamps and AI outputs get real columns.
3. **`custom` is for user-defined data only.** The moment a product feature
   filters, sorts or joins on something, it earns a column.

**Relations are the one place to be maximally generic.** JSONB cannot model
many-to-many well. Take HubSpot's shape: a first-class `associations` table
(`from_type, from_id, to_type, to_id, label`), many-to-many by default with
optional labels and configurable limits, surfaced bidirectionally with zero
configuration (Attio's behavior — linking a Person to a Company immediately
populates both sides).

## 2. Modularity

**Genuinely modular:** EspoCRM, Odoo. **Modular at high cost:** Erxes.
**Monolith with a settings page:** SuiteCRM, Monica, Atomic CRM (deliberately —
it is a template you fork). **Runtime-configurable but not extensible:** Twenty.

- **EspoCRM** is the model to follow. The seam is dropping JSON into
  `custom/Espo/Modules/{Module}/Resources/` — `module.json` for load order,
  `routes.json` for API, `metadata/scopes/*.json` and `entityDefs/*.json` for
  entities — and metadata is **recursively deep-merged** over core. A module can
  add an entity, fields, layouts, ACL scope and routes without touching core.
  Real modularity at a cheap seam.
- **Odoo** is the most powerful and most expensive. `_inherit` without `_name`
  extends a model *in place*; views are patched by XPath. The cost is that
  customizations couple to core internals and every major version churns ORM
  semantics — reported migration effort for heavily customized databases runs
  ~3x longer and ~5x more expensive.
- **Erxes** went GraphQL Federation + tRPC microservices + Module Federation
  micro-frontends on MongoDB. Real isolation, but distributed-systems tax before
  you have users.
- **Twenty** is configurable at runtime but has no server-side plugin seam;
  extending behavior means forking. Its modularity is *data-model* modularity,
  not *code* modularity.

**Decision:** EspoCRM's shape, modernized. One in-process **event bus**
(`record.created`, `record.updated` with field-level diffs) plus outbound
webhooks covers 90% of cases. Skip a marketplace. Add a workflow engine only
after the bus exists — it should be the bus's first *consumer*, not a parallel
mechanism.

## 3. Permissions

A small multi-user team needs exactly four things:

1. Role → object → CRUD grid (EspoCRM's `create/read/edit/delete/stream` axes).
2. Record visibility **all / team / own / none**, with multiple roles merging
   *permissively*.
3. Field-level read/edit masking for a handful of sensitive fields.
4. A hard admin/non-admin split for settings.

**Skip:** sharing rules, territory hierarchies, criteria-based sharing,
org-wide defaults, manual record shares. Salesforce's sharing model exists for
5,000-seat orgs and is the single most-regretted complexity in CRM
implementations.

### Enforcement — a real fork

- **App-layer filtering** (SuiteCRM/EspoCRM/Monica): easy, but every new query
  path is a potential leak, and background jobs, exports and AI agents routinely
  bypass it.
- **Predicate injection at one choke point:** testable, keeps the planner fully
  informed.
- **Postgres RLS:** isolation at the storage layer, so no code path can leak —
  including `psql`, cron jobs and future AI tooling.

Published RLS experience is favorable if the rules are obeyed: overhead of
**1–6%** (single-row +2.4%, filtered list +3.2%, 3-table join +5.9%),
sub-millisecond policy evaluation at 50M rows / 10K tenants, `SET LOCAL` under
0.1ms ([Nile](https://www.thenile.dev/blog/multi-tenant-rls),
[Fritzsche](https://ricofritzsche.me/mastering-postgresql-row-level-security-rls-for-rock-solid-multi-tenancy/)).
The documented killers: **missing composite indexes with `tenant_id` leading**
(two orders of magnitude slower), forgetting `FORCE ROW LEVEL SECURITY` (the
table owner sees everything), and `BYPASSRLS`/superuser connections. Add:
poolers in transaction mode require `SET LOCAL` *inside* the transaction.

**Decision:** RLS for the **tenant boundary only** — cheap, absolute, and it
makes cross-tenant bugs structurally impossible. Record-level visibility
(own/team) goes in the query layer, because those predicates change per query and
are far easier to index and debug as SQL you can read.

## 4. Contact sync, and relationship intelligence

**Bidirectional sync is a trap.** Almost no open-source CRM does it; they do
one-way import or per-user OAuth pull. Systems that do it resolve conflicts by
declaring a **source of truth per field or per record**, never by merging.

### Microsoft Graph gotchas

- `GET /me/contacts` hits **only the default Contacts folder**; subscriptions
  likewise. Enumerate `contactFolders` and run a delta cycle per folder.
- Delta token expiry is **not a fixed TTL** — it depends on an internal cache's
  size, and old tokens are evicted when capacity is exceeded. Expect
  `syncStateNotFound` / `410 resyncRequired` at arbitrary times.
- Treat `@odata.deltaLink` as **opaque**; never reconstruct it. Crashing
  mid-round with `nextLink` stored but not the prior `deltaLink` means a full
  resync is the only correct recovery.
- IDs are **not stable** without `Prefer: IdType="ImmutableId"`, and break when
  an item crosses mailboxes. Existing subscriptions cannot be upgraded.
- 1,000 active Outlook subscriptions per mailbox across all apps.

### Google People gotchas

- `syncToken` expires **7 days** after the full sync; expired use returns **410
  with `EXPIRED_SYNC_TOKEN`**, and recovery is a full sync with no token.
- The first page of a full sync draws on a **separate, fixed, non-increasable
  quota** — exceeding it returns 429. A bug triggering mass re-syncs locks you
  out. This is the real operational risk.
- Deletions arrive as `PersonMetadata.deleted = true`, present **only in
  incremental responses**. After a full resync deletions are invisible; you must
  diff against the local set.
- `otherContacts` is a separate resource with its own tokens and a restricted
  `readMask`.

### Design implications

Store per-account `(provider, account_id, folder_id, delta_token,
token_acquired_at, last_full_sync_at)`; treat token loss as a **normal path**
with a tested full-resync + tombstone-diff routine; store the provider's
immutable id **and** etag per linked record in a link table rather than
overloading the primary key; resolve conflicts field-wise using `updated_at` plus
a per-field `source`; match on a normalized email set first, then
`(normalized name + domain)`, **never name alone**.

### The Affinity/Clay model

Affinity creates records for **every person and company the team has interacted
with**, automatically, from mail and calendar, then scores relationship strength
from communication patterns, surfaces warm-intro paths, and flags relationships
going quiet. Clay does the same for individuals.

The UX insight is not auto-import. It is that **the CRM is a derived view over an
interaction log**, so the database is complete on day one and never rots — the
failure mode of every typed-in CRM is that people don't type.

The schema consequence is the largest single finding in this document: **the
primitive is not `contact`, it is `interaction`** (`from, to[], cc[], at, type,
thread_id, direction, subject_hash`). Persons and organizations are *materialized*
from interactions, with an `is_derived` flag and a promotion step when a human
touches one. Attio encodes this with a native `interaction` attribute type.
Relationship strength, "going cold" and warm-path graph queries all fall out of
the interaction table for free; **none of them are computable from a contacts
table**. Design for it from the start even if it ships later — retrofitting an
interaction log under an existing contacts model is the hard version.

## 5. UI/UX structure

The convergent modern layout (Attio, Twenty, and the Linear lineage):

- **Left rail:** object nav, with **saved views nested under each object** —
  views are first-class, persisted, shareable objects owning their own filters,
  sorts, visible columns and grouping.
- **Center:** a spreadsheet-grade table with **inline in-cell editing**,
  keyboard navigation, resizable/reorderable columns, and a view-type switcher
  (Table / Kanban / Calendar / Timeline). Kanban is a *rendering of the same
  view* grouped by a status attribute, not a separate screen.
- **Record page, three regions:** left summary rail (inline-editable,
  collapsible groups), center **timeline** merging emails, meetings, notes and
  field changes into one stream, right/lower related-records blocks.
- **Command palette (Cmd-K)** as the primary navigation and action surface.
- **Quick-create everywhere**, optimistic updates.

What Attio and Twenty do that SuiteCRM and Salesforce Classic do not: **no edit
mode** (every field is directly editable in place); **the table is the app**, not
a list-then-detail funnel; **views are objects users create**, not admin-configured
list views; **keyboard-first with a command palette**; and **relations are
navigable inline and bidirectional with zero configuration**.

From Pipedrive, copy exactly two things: the drag-and-drop stage board as the
default deal view, and **deal rotting** — cards degrade visually after N days
without activity, converting pipeline hygiene from a manager's report into an
ambient signal. Highest ROI-per-line-of-code feature in CRM UX.

## 6. AI features that actually landed (2025–2026)

- **AI attributes / field-level agents.** Attio's AI Attributes are custom fields
  on any object that auto-fill via web research, classification into ICP tiers,
  summarization, or prompt-completion over existing attributes. The single
  most-copied 2025 pattern.
- **Suggestion, not autonomy.** HubSpot's Smart Deal Progression proposes field
  updates and **requires manual approval for every one**; content agents queue
  drafts for review; the 2026 "audit card" records which properties an AI changed
  and their previous values.
- **Provenance/lineage as a first-class concern.** Salesforce bought Informatica
  (closed Nov 2025) specifically for lineage under Agentforce; industry surveys
  put data quality and lineage at **42%**, the top blocker to agentic readiness.
- **Semantic search / RAG on pgvector with HNSW**, comfortably adequate below
  ~1M vectors and fine into tens of millions.

### What this demands of the schema — hard requirements, not future work

1. **Field-level provenance.** Every writable field needs
   `(value, source ∈ {human, sync:google, sync:graph, ai:<agent>,
   enrichment:<vendor>}, confidence, generated_at, model, prompt_version)`, as a
   `field_provenance` table plus an append-only `field_history`. **Not in the
   JSONB blob.**
2. **A pending-changes/approval queue** — `proposed_changes(record_ref,
   field_key, current_value, proposed_value, rationale, citations[], agent,
   confidence, status, reviewed_by, reviewed_at)`. This is the substrate for
   AI suggestions *and* sync conflict resolution. One mechanism, two use cases.
3. **Never let AI silently overwrite human-entered values.** Precedence: human >
   provider sync > AI. AI writes to human-owned fields go to the queue.
4. **`pgvector` in a separate `embeddings` table**, HNSW-indexed, keyed by
   content hash so re-embedding is idempotent and a model migration is a
   backfill. **Vectors never go on record tables.**
5. **The interaction log is also the RAG corpus.** Without it, AI features are
   summarizing form fields.

## 7. Ranked recommendations

**Copy, highest confidence first:**

1. Fixed core tables + JSONB `custom` + registry, **with index promotion as a
   required capability**.
2. A first-class `associations` table (HubSpot shape) with Attio's automatic
   bidirectional surfacing. Never model relations in JSONB.
3. An `interactions` table from day one, with persons/orgs derivable from it.
   Highest long-term leverage; near-impossible to retrofit.
4. EspoCRM's permission model verbatim, with **RLS for `org_id` only** and query-
   layer predicates for record visibility.
5. EspoCRM's module shape, with an in-process event bus + outbound webhooks as
   the seam.
6. Attio's UI structure — saved views as objects, no edit mode, table-as-app,
   Cmd-K, three-region record page — plus Pipedrive's deal rotting.
7. Provenance + approval queue + pgvector, designed in now, populated later.

**Avoid:**

1. **Per-workspace schemas / runtime DDL.** Elegance in exchange for a permanent
   second migration system, non-atomic upgrades and Postgres catalog pathology.
2. **Pure EAV or side-table-per-module.** Attio's version works because they
   built a bespoke typed value store and query engine; Salesforce's answer to the
   same problem was a custom optimizer.
3. **Odoo-style in-place model monkey-patching.** Elegant, and it makes every
   upgrade a project.
4. **Microservices / plugin federation at this stage.** Monolith + modules +
   event bus.
5. **Enterprise sharing models, marketplaces, and a workflow engine before an
   event bus.**
6. **Naive bidirectional contact sync.** One-way with tombstones first;
   write-back per-field behind the approval queue.

**The genuine architectural forks:**

- **System of record vs. derived view over interactions.** Attio/Twenty say
  record; Affinity/Clay say derived. Choosing derived changes the primary table
  and is not cheaply reversible. **Chosen: derived-with-promotion.**
- **RLS boundary placement.** Tenant-only (chosen) vs. full record-level RLS
  (makes team/own predicates invisible to your own debugging) vs. none
  (background jobs and AI agents become the leak surface).
- **Extensibility strength.** Declarative-only modules (safe, upgrade-proof) vs.
  code-loading server modules (powerful, versions you into Odoo's problem).
  **Chosen: start declarative**, add a narrow versioned hook API when a real
  module needs it.
- **Custom objects.** If user-defined *objects* are ever wanted, the choice is
  Twenty's DDL route (rejected) or a generic `records` table alongside the fixed
  core — a two-tier model. **Chosen: two-tier**, because it is far easier to build
  in at the start than to bolt on.
