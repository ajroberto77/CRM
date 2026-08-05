# Integration Notes

This is a framework for the merge conversation, not a decision made on this
system's behalf. It collects what this audit found that most directly bears
on combining this CRM with a colleague's parallel investor-tracking system,
and poses the open questions explicitly rather than assuming an answer.

## Where the two systems most obviously need to agree

### 1. Identity, conflicts, attribution, history

`02-data-model.md`'s opening section answers these four questions for this
CRM in detail. Summarized, for direct comparison against however the sibling
system answers them:

| Question | This CRM's answer |
|---|---|
| How are duplicate people/companies avoided? | Normalized channel-identity matching (email/phone/handle), never name matching. No fuzzy-review workflow. |
| How are conflicting facts about the same thing handled? | **Not structurally handled** — last write wins, except for period-shaped facts (`metric_fact`), which don't collide across time by construction. |
| How is "who said this" tracked? | Two separate mechanisms: `core.events` attributes every *field change*; `interaction.owner_id` scopes *message-body* visibility. Neither is "who told us this specific fact." |
| How does a changing assessment of someone get preserved? | Event log (field diffs), dated association history (`valid_from`/`valid_to`, never deleted), and the immutable interaction log itself. No dedicated narrative/assessment-history field. |

**Before merging**: if the sibling system has a *stronger* answer to any of
these four (a real conflict-resolution mechanism, an explicit
who-told-us-this field, a narrative assessment history), that's a genuine
capability gap worth porting into this CRM as a registered mechanism — R1's
"search for the existing one before writing a second" applies to *importing*
a pattern from a sibling system exactly as much as to writing new code from
scratch. If this CRM's answer is stronger, the reverse.

### 2. Where each system's strength actually is

This audit's own read on the split, based on what's built vs. designed:

- **This CRM's strength**: the relationship/association model (one legal
  entity, many simultaneous roles, one history), the permission/multi-tenancy
  layer, the registry-driven "new entity/role/provider is additive, not a
  fork" discipline, and — as of this session — a real asset-management
  vertical with public/private classification, prospective-vs-actual
  portfolio tracking, and compliance document-gating.
- **The concrete gaps this audit found** (both detailed in
  `05-messaging-channels.md` and `06-sync-and-jobs.md`): no mail ingestion
  pipeline at all, and no path from an inbound Signal/Telegram message into a
  logged interaction. The scheduling-extraction → approval-queue →
  calendar-write pipeline, contact derivation, and semantic search are all
  fully built and tested *assuming interactions exist* — they just don't have
  a live producer for email/messaging content today.
- If the colleague's system's stated strength really is data collection, the
  natural, lowest-friction integration shape is: **the sibling system (or a
  new adapter modeled on its data-collection approach) becomes the producer
  that fills that exact gap** — writing `core.interactions` rows (through the
  existing generic `repository.create()` path, not a new one) rather than
  either system rebuilding what the other already has.

### 3. Schema reconciliation

`02-data-model.md` lists every entity and association role this CRM
registers. Before merging, the concrete exercise is a field-by-field
comparison against the sibling system's schema (once the colleague's own
documentation of it exists) — specifically:
- Which fields the sibling system tracks that have no equivalent here (candidates
  for a new `custom_field`, or a new core/module field if the field is
  universal enough to be domain-neutral).
- Which of this CRM's fields the sibling system has no equivalent for, and
  whether that's a gap on their side or evidence the field isn't actually
  needed.
- Whether the sibling system's notion of "investor," "commitment," or similar
  vocabulary maps onto this CRM's association-role model (a dated relationship
  on an ordinary organization/person) or a fixed-schema table — if the
  latter, that's the specific place R6 ("a role is not an entity type") would
  need to be either applied or deliberately overridden, with the tradeoff
  made explicit rather than silently forked.

## A confidentiality note, raised generally (not specific to any repo this session has access to)

If real investor names, commitments, or deal terms are going to start living
in either system's version control history — as opposed to schema/structure
only — that is confidential LP data, and where it's allowed to live (a
private repo on a cloud host, a self-hosted git server, local-only) is a real
decision with compliance implications that deserves an explicit answer before
merge, not an assumption. This CRM's own posture on the equivalent question is
architectural, not incidental: PostgreSQL RLS keeps tenant data server-side
and per-org; nothing about this CRM's design puts real LP data into a git
repository in the first place. Whatever the sibling system's answer is here
should be resolved as its own decision, explicitly, before the two are
combined.

## What this document does not attempt

It does not propose a merged schema, does not decide which system's
conventions win where they conflict, and does not recommend a migration
path — those all require the colleague's own system to be documented with the
same rigor this audit applied here (per the seven-part request the user
relayed, which remains theirs to execute against their own repository). This
document's job is to make sure this CRM's side of that eventual comparison is
accurate and complete.
