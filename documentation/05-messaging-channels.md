# Messaging / Channels Subsystem

## 1. `server/channels/` — structure and what each file actually implements

This is R3's fifth dispatch axis. Per `CLAUDE.md`, the axis table names three
adapters (signal, telegram, email), but **only two are implemented**:
`dispatch.py`'s `PROVIDERS = ("signal", "telegram")` (`server/channels/dispatch.py:25`).
There is no `server/channels/email.py` and no email branch anywhere in
`dispatch.py` — `dispatch.py:7` explicitly calls email "a third provider" still
to be added ("Adding a third provider (email, per R3's table) is one new
`<provider>.py` file... plus one `elif`"). **Treat "email" as an aspirational
row in the axis table, not a shipped adapter.**

- **`server/channels/dispatch.py`** — the one dispatcher (R3). No provider name
  appears at module scope; each of the two public functions does a lazy,
  per-branch `import` of the concrete adapter:
  - `send(org_id, user_id, text, *, thread_to=None) -> str` (`dispatch.py:45`):
    looks up the user's linked channels via `user_channels.list_channels()`,
    picks the earliest-linked one (`created_at` order) if more than one exists,
    dispatches to `signal_cli.send()` or `telegram.send()`, and returns the
    provider's opaque message id. Gated by `config.messaging_send_enabled()` —
    raises `MessagingDisabledError` rather than no-op'ing (safety rules 3 and 9,
    see §7). Raises `NoLinkedChannelError` if the target user has no linked
    channel.
  - `receive_commands() -> list[tuple[org_id, user_id, text, quoted_id]]`
    (`dispatch.py:76`): polls **every** provider exactly once per call (not once
    per org), resolves each raw sender through `user_channels.resolve_user_any_org()`,
    and silently drops unrecognized senders. One provider's exception is
    swallowed (`except Exception: continue`, `dispatch.py:112`) so Telegram
    being unconfigured never blocks Signal.

- **`server/channels/signal_cli.py`** — ported near-verbatim from JA's
  `messaging.py`. `send()` (`:91`) shells out to
  `signal-cli -u <bot number> send <dest> -m <text>`, adding
  `--quote-timestamp`/`--quote-author` for threaded replies; returns
  signal-cli's stdout (its own message timestamp) as the opaque id.
  `receive_commands()` (`:137`) runs
  `signal-cli -o json -u <bot number> receive -t 5` and parses each JSON
  envelope via `_extract_command()` (`:118`), returning `(sourceNumber, text,
  quoted_id)` for `dataMessage` text messages only (skips receipts/typing
  indicators/group messages). Also ports JA's `libsignal*` temp-dir leak fix:
  `_cleanup_stale_libsignal_tmp()` (`:54-88`), rate-gated to once per hour.

- **`server/channels/telegram.py`** — new work, no JA equivalent. Uses Bot API
  long-polling (`getUpdates`) rather than a webhook. `chat_id` doubles as both
  destination and sender identity for 1:1 bot chats. `receive_commands()`
  (`:100`) tracks `_last_update_id` **in-memory only** and advances it past
  every update seen, whether or not it was a recognized command — a process
  restart re-delivers whatever Telegram is still holding (bounded by its own
  ~24h retention). Documented as an accepted first-cut limitation: a
  redelivered stale "yes/no" just hits `AlreadyDecidedError` downstream and is
  a no-op.

## 2. The two-way command loop (ported from JA) and the command grammar

**Flow**: `server/jobs/message_poller.py:poll_once()` →
`dispatch.receive_commands()` (one poll of both providers, sender resolved to
`(org_id, user_id)`) → `server/channels/commands.py:dispatch_command(org_id,
user_id, text, quoted_id)`.

**The grammar (safety rule 1's "exact vocabulary")** lives entirely in
`server/channels/commands.py:33-34`:
```python
_APPROVE_WORDS = {"yes", "y", "approve", "ok", "okay"}
_DECLINE_WORDS = {"no", "n", "decline", "cancel", "skip"}
```
`dispatch_command()` lowercases/strips the inbound text and does a plain
set-membership test — no LLM, no fuzzy matching. That is the **entire command
vocabulary today**: there is no command for creating/editing/searching records,
listing pending items, etc. — the loop only decides pending `proposed_changes`
(approve/decline).

**Quote resolution** — the target proposal is found *only* via
`quoted_id → proposed_changes.notification_message_id`
(`proposals.find_by_notification_message_id()`, `commands.py:63`). There is
deliberately **no "most-recent-pending" fallback** — no quote, an unresolved
quote, a proposal not owned by that `user_id`, or one no longer
`status == "pending"` are all treated identically as "not a recognized
command" (`commands.py:44-58, 66-68`). This directly ports JA's live incident
(a conversational reply to a stale notification got redirected into an
unrelated pending item and sent as a real email — `docs/SOURCE-PATTERNS.md`).

**Compare-and-swap claim**: the actual decision is `proposals.approve()`/
`proposals.decline()` (`commands.py:54-56, 72`), which funnel through
`proposals._set_decision()` — this re-reads current status, and its
`repository.update(..., if_unmodified_since=current["updated_at"])` acts as the
optimistic-concurrency compare-and-swap; a race (dashboard click vs. Signal
"yes") raises `repository.Conflict`, translated to `AlreadyDecidedError` and
surfaced as "not a recognized command."

## 3. `server/core/derivation.py` — person materialization from interactions

Wired in as an event subscriber, not a poller: `install()` subscribes
`_derive_contacts` on `interaction` `CREATED` events, and
`_promote_on_human_edit` on `person`/`organization` `UPDATED` events.

- **`resolve_or_create_person(principal, channel_kind, raw_value)`** (`:29`):
  normalizes via `identity.normalize()`; looks up an existing `contact_channel`
  row by `(kind, value_normalized)`; if none, creates a `person` with
  `source="derived", is_derived=True` and `full_name=raw_value` (the **raw**,
  not normalized, value), plus a matching `contact_channel` row. Returns `None`
  for an unparseable value.
- **`_derive_contacts`** (`:83`): maps `interaction.kind` → channel kind via
  `_CHANNEL_KIND_FOR_INTERACTION_KIND = {"email": "email", "call": "phone",
  "sms": "phone"}` — `meeting`/`chat`/`other` are skipped ("no reliable channel
  shape... rather than guessed at"). For every `from_channel`/`to_channels`
  value it calls `resolve_or_create_person()` then
  `interactions.add_participant()`, running as
  `permissions.system_principal(org_id, "derive contact from interaction")`.
- **Promotion** — `_promote_on_human_edit` (`:118`): if `event.before.is_derived`
  and `event.after.is_derived` are both still true and the actor is **not**
  system-originated, it writes `is_derived=False` via a system principal —
  "derived record promotes the moment a human touches it" (`docs/DESIGN.md`).
- **Known gap, documented in the module** (`:41-49`): `resolve_or_create_person`
  is select-then-insert, not atomic — a race between two callers materializing
  the same brand-new contact concurrently can produce two `person` rows. Flagged
  as low-severity, left as a follow-up.

## 4. Where interactions are actually created — an important gap to flag

`interaction` is a normal registry entity (table `core.interactions`), created
only through `repository.create(principal, "interaction", ...)` — the generic
CRUD path (R4).

**There is currently no code path that turns an inbound Signal/Telegram
message, or an inbound email, into an `interaction` row automatically.**
Specifically:
- `server/providers/mail.py` (R3's Mail-axis dispatcher, named in `CLAUDE.md`'s
  table) does not exist in `server/providers/` — only `calendar.py`,
  `contacts.py`, `esign.py` and their adapters are present. There is no
  mail-polling job in `server/jobs/` either (only `sync_poller.py`, which is
  contact sync, not mail).
- `server/channels/message_poller.py`'s loop only ever calls
  `commands.dispatch_command()` — it never creates an `interaction`. Inbound
  Signal/Telegram text is either an approve/decline command (§2) or is
  silently ignored; it is not logged as an interaction.
- `scheduling_pipeline.py:_on_interaction_created` assumes
  `interaction.kind == "email"` interactions will show up from *somewhere* (a
  future mail-ingestion writer), but that writer is not yet built.

So today, `interaction` rows (and the derivation they trigger) come from
whatever calls the generic repository/API directly — manual logging via the
UI/REST API, or test fixtures — not from an automated channel/mail ingestion
pipeline. `contact_channels` identity resolution works uniformly whenever an
interaction *is* created, but the "importing a mailbox materializes people
nobody typed" milestone (M2 in `docs/DESIGN.md`) is not yet wired to a live
inbound source. **This is the single largest gap between design intent and
shipped code in the messaging subsystem, and a natural seam for the sibling
project's data-collection strength to plug into.**

## 5. Polling/queue mechanism for channels

Two independent standalone sleep loops exist (M7's general work queue,
`server/jobs/queue.py` + `workers.py`, doesn't yet register channel work —
`jobs/handlers.py:register_all()` only wires `event_redelivery` and
`embed_interaction` job types):

- **`server/jobs/message_poller.py`** — the Signal/Telegram command loop.
  `poll_once()` calls `dispatch.receive_commands()` once, then processes each
  tuple through `commands.dispatch_command()` inside its own `try/except` so
  one bad command doesn't stop the rest of the batch. `run_forever(interval_seconds=15)`
  wraps `poll_once()` in another `try/except`. Run standalone:
  `python3 -m server.jobs.message_poller`.
- **`server/jobs/sync_poller.py`** — a different axis (contact sync from
  Microsoft/Google connected accounts), included here only because
  `derivation.resolve_or_create_person()` is shared between it and
  `derivation.py`'s interaction-driven path.

Both channels are **poll-driven, not webhook-driven** — Telegram uses
long-polling `getUpdates` rather than a registered webhook, and Signal uses
`signal-cli receive` blocking for 5 seconds per call.

## 6. Safety rule 4 — no data loss on transient failure — as applied to channels

Rule 4's classic form (never advance a delta cursor past a failed item) is
**N/A in its literal shape** for the message poller: unlike a mail/contact sync
delta cursor, a command returned by `receive_commands()` is *already consumed
at the provider* — signal-cli's `receive` and Telegram's `getUpdates` offset
both acknowledge on fetch, so there is no cursor to hold back. The mitigation
actually implemented is: **each command is processed in its own
`try/except`** so one command's processing failure can't cost the rest of that
poll's batch, and the failure is logged, not swallowed silently.

The rule's literal form *is* implemented, correctly, for the sibling axis this
shares infrastructure with: `sync_poller.py:93-115` (`sync_account()`) only
calls `contacts_dispatch.commit_cursor(...)` after every contact in the
fetched batch resolves successfully; a mid-batch exception returns `False`
immediately without committing the cursor, so the next cycle re-fetches the
same batch.

## 7. Safety rule 9 — destructive paths default off — applied to channel-triggered actions

- **The messaging send gate**: `config.messaging_send_enabled` defaults to
  `false`, checked at the single choke point `dispatch.send()` and **raises**
  `MessagingDisabledError` rather than no-op'ing.
- **The calendar write gate**, which is what a channel-approved scheduling
  proposal ultimately triggers: `config.calendar_write_enabled` also defaults
  `false` and is checked first in `scheduling_pipeline._route_proposal()` —
  before the approval-mode/trust logic that might otherwise auto-accept.
- **Manual is the default approval mode**: `approval.get("approval_mode",
  "manual")` — only `"auto"` or `"confidence"` (above threshold) execute
  automatically; everything else stays `pending` and (best-effort) notifies
  the owner over their linked channel.
- **`core/proposals.py`** is the one approval queue for **all**
  channel-originated writes — a channel "yes"/"no" never writes to a
  calendar/contact/etc. directly, it only flips `proposed_changes.status`.
  Worth a direct follow-up look: the auto-accept paths clearly execute the
  actual write, but whether a *manually*-approved-via-channel decision has a
  subscriber that then performs the calendar write is not unambiguous from
  `commands.py` alone — `core/events.py`'s subscriber list for `proposed_change`
  is the place to confirm this wiring is complete end to end.

## 8. Connected accounts / connected channels

Two distinct, deliberately separate concepts and API surfaces:

- **`server/api/channels.py`** — Signal/Telegram identity linking (M6).
  `_LINKABLE_KINDS = ("signal", "telegram")` — narrower than
  `identity.CHANNEL_KINDS` (which also has email/phone/handle for
  `contact_channels`' broader third-party use). Routes: `GET /channels` (list
  caller's own), `POST /channels/link`, `DELETE /channels/{channel_id}`. Never
  accepts a target `user_id` — always scoped to `current_principal()`, no
  admin override, no reassignment surface.
- **`server/core/user_channels.py`** — backing store, `core.user_channels`,
  distinct table from `contact_channels` (which maps a channel to a
  third-party `person`, never to a CRM login). `link_channel()` is idempotent
  for the same `(user, kind, value)`, raises `AlreadyLinkedError` if already
  claimed by a *different* user — never silently reassigns.
  `resolve_user_any_org()` is the reverse lookup `dispatch.receive_commands()`
  needs since Signal/Telegram polling is deployment-wide, not per-org.
- **`server/api/accounts.py`** + **`server/core/accounts.py`** /
  **`server/core/account_link.py`** — a completely different axis: OAuth-linked
  **Microsoft/Google mail/calendar/contacts** accounts (M4), not messaging
  channels. `account_link.py` is explicitly *not* one of R3's six axes — it's
  shared OAuth-linking infrastructure underneath contacts/calendar/(future)
  mail. It centralizes the one exception-translation boundary
  (`LinkUpstreamError`) so provider-specific exceptions never escape past
  `start_link`/`complete_link`.
