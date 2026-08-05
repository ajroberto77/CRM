# Mail/Calendar/Contacts Sync + Job Queue

**Important finding up front: the mail axis does not exist yet as code.**
`CLAUDE.md`'s R3 table names `server/providers/mail.py` as the mail
dispatcher, but no such file exists, nor do `microsoft_mail.py`/
`google_mail.py` adapters. `docs/DESIGN.md`'s milestone table confirms this
isn't an oversight: M4 is "OAuth + Microsoft & Google **contact** sync," M5 is
"Calendar + scheduling extraction," and there is no mail-sync milestone at
all. `server/db/schema.py` documents the gap explicitly in a comment on
`core.sync_cursors`: *"a future synced resource (mail, in a later milestone)
needs no migration here, just a new `resource` value"* — and indeed `resource`
is currently `CHECK (resource IN ('contacts','calendar'))`, with no `'mail'`
option yet. Everything below reflects what's actually implemented (contacts +
calendar), and notes the mail gap precisely wherever it's relevant. **This is
the second-largest concrete gap in the codebase (after messaging's missing
inbound pipeline, see `05-messaging-channels.md`) and the most natural place
for a data-collection-focused sibling project to plug in.**

## 1. `server/providers/` — dispatchers and adapters

**Calendar dispatcher — `server/providers/calendar.py`** (R3 axis,
`PROVIDERS = ("microsoft", "google")`):
- Adapter interface: `check_event(org_id, account, date, proposed_title=None) -> dict`,
  `create_event(org_id, account, **kwargs) -> str`,
  `update_event(org_id, account, provider_event_id, **kwargs) -> str`.
- `calendar.py` itself also hosts logic shared by both adapters (not
  vendor-specific, so it lives at the dispatch layer, R1): `account_timezone()`
  (falls back to UTC on bad/missing tz), `local_day_utc_window()` (safety rule
  7 — see §5), and `pick_update_target()` (word-overlap-minus-stopwords
  heuristic deciding create vs. update).
- `microsoft_calendar.py`: Graph adapter. Uses `/me/calendarView` with
  `startDateTime`/`endDateTime` query params (always UTC regardless of the
  `Prefer: outlook.timezone` header — that header only affects response
  rendering), `PATCH` (not `PUT`) for updates.
- `google_calendar.py`: Calendar API v3 adapter, `timeMin`/`timeMax` RFC3339
  params, `summary` field for title, defaults to calendar id `"primary"`.
- Capabilities: check/list a day's events, create an event, update
  (PATCH-merge) an existing event. **No delete capability exists in either
  adapter.**

**Contacts dispatcher — `server/providers/contacts.py`** (`PROVIDERS =
("microsoft", "google")`):
- Adapter interface: `fetch_contacts(org_id, account) -> (contacts, next_cursor)`.
  Every adapter normalizes into the shared shape `{provider_object_id,
  display_name, emails, phones, company_name, deleted}` so `sync_poller.py`
  never branches on vendor.
- Cursor persistence (`commit_cursor()`) lives in the dispatcher, delegating
  to `server/core/accounts.py:commit_sync_cursor()`.
- `microsoft_contacts.py`: Graph delta query `/me/contacts/delta`, cursor is
  Graph's opaque `@odata.deltaLink`; a raw item with `"@removed"` maps to
  `{deleted: True}`.
- `google_contacts.py`: People API `people/me/connections`, cursor is Google's
  opaque `syncToken`. Handles Google's 410 Gone (expired sync token) by
  retrying with an empty token for a full resync (safety rule 5, see §4).
- Capabilities: paginated delta fetch of contacts (with soft-delete markers),
  **no create/update/delete back to the provider** — contacts sync is
  read-only by design ("this milestone only syncs in, it never writes back").

**Underlying transport adapters** (not one of the six R3 axes, shared infra
underneath contacts+calendar+oauth):
- `microsoft_graph.py`: PKCE OAuth against `/common` tenant, `mint_access_token()`
  minted fresh every call (never cached), token rotation-on-refresh saved
  back via `accounts.save_credentials()`.
- `google_api.py`: structural mirror, confidential-client OAuth2+PKCE. Google
  does not always rotate the refresh token — only saved back if returned.

**No `server/providers/mail.py`, no `microsoft_mail.py`/`google_mail.py`.**
Consequently there is currently no code path that lists a mailbox's messages,
sends mail, or runs a mail delta sync — despite `core.interactions`
(`kind='email'`) and the entire scheduling pipeline (§7) being fully built to
consume email-shaped interactions the moment such a producer exists.

## 2. `server/jobs/` — work queue architecture

Two distinct mechanisms coexist deliberately:

**A. Standalone poll loops** (pre-M7, still used for contacts sync and
messaging), each its own `while True: poll_once(); sleep()` process, sharing
only `server/jobs/__init__.py:all_org_ids()` (queries `auth.identities`
directly via `pool.system_transaction()`, since RLS would otherwise return
nothing with no tenant GUC set):
- `sync_poller.py` — contacts sync, 300s default interval. `python3 -m
  server.jobs.sync_poller`.
- `message_poller.py` — Signal/Telegram command loop, 15s default interval.
  `python3 -m server.jobs.message_poller`.

**B. The generic M7 work queue** (`jobs.work_queue`, one table for every job
type — a deliberate deviation from CATO's one-table-per-job-type pattern):
- `enqueue(job_type, payload, max_attempts=5)` — `INSERT ... RETURNING *` into
  `jobs.work_queue`, always via `pool.system_transaction()` since the table is
  UNSCOPED; any tenant data needed by the handler travels inside `payload`.
- `claim_batch(worker_id, job_types, batch_size)` — ported from CATO:
  `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED AND
  status='pending' ORDER BY available_at LIMIT batch_size) AND
  status='pending' RETURNING *`. The `(status, available_at)` composite index
  is called out as a ~5400x speedup in CATO's own measurement.
- `mark_done(job_id)` / `mark_failed(job_id, error)`: on failure, `attempts`
  increments; if `attempts >= max_attempts` the row moves to terminal
  `dead_letter` (never auto-reclaimed), otherwise back to `pending` with
  `available_at = now() + backoff` where `_backoff_seconds(attempts) =
  min(600, 2**attempts)`.
- `reclaim_stale_claims(stale_minutes)`: resets any `claimed` row whose worker
  crashed mid-job back to `pending` after the timeout (default 45 min), called
  every loop iteration.
- Worker registration/heartbeat: `register_worker()`, `heartbeat()`,
  `check_stop_requested()`, `mark_worker_stopped()`, `reap_stale_workers()` —
  `worker_id` is `hostname-pid`, so a restart is a new identity and old rows
  age out.

**The worker loop** — `server/jobs/workers.py:run_forever()`: each iteration
checks `check_stop_requested()`, calls `reclaim_stale_claims(45)`, every 120s
runs `event_redelivery.sweep()`, calls `run_once(worker_id, batch_size=10)`
which claims and dispatches to per-`job_type` handlers registered via
`register_handler()`, heartbeats every 15s, sleeps 5s only if nothing was
claimed. Run via `python3 -m server.jobs.workers`.

**`server/jobs/handlers.py`** — the single wiring point (mirrors
`core/modules.py`'s `install_enabled_modules()` shape): registers
`event_redelivery.JOB_TYPE -> handle_redeliver_event` and
`interaction_embeddings.JOB_TYPE -> embed_interaction.handle_embed_interaction`.

**Current M7 job types**: `redeliver_event` (drains `core.events`'s
at-least-once outbox — known, documented tradeoff: no de-dup check before
enqueuing, acceptable because subscribers must be idempotent/commutative) and
`embed_interaction` (re-fetches the interaction, chunks+embeds its body,
stores with `visibility_user_id` = the interaction's owner — safety rule 10).
**No mail-sync job type exists.**

## 3. Sync jobs — contacts (mail path not yet built)

- `poll_once()`: for every org, for every connected account with `status in
  ("active", "error")`, calls `sync_account(account)`.
- `sync_account(account)`: calls `contacts_dispatch.fetch_contacts()` to get
  `(contact_list, next_cursor)`; on exception, sets account `status='error'`
  and returns `False` **without ever touching the cursor**. Otherwise,
  iterates `_sync_one_contact()` per contact; **on the first per-contact
  exception it stops immediately and returns `False`**, again without
  committing the cursor. Only once every contact in the batch is processed
  does it commit the cursor and flip `status` back to `'active'` if it had
  been `'error'`.
- `_sync_one_contact()`: deletes drop the `core.sync_links` row rather than
  deleting the derived CRM person — a provider delete must never cascade into
  deleting a person who's accrued real human edits. Otherwise resolves/creates
  a person via `derivation.resolve_or_create_person()` using the contact's
  first email or first phone as the identity signal, optionally upgrades a
  still-`is_derived` person's `full_name` (never overwrites a human-promoted
  name), and records the mapping via `accounts.create_sync_link()`.

**Where `core/derivation.py` gets invoked**: `resolve_or_create_person()` is
called directly by `sync_poller.py` for contacts (needs the person id back to
write `core.sync_links`). For interactions (which would include mail, once
built), `derivation.py` instead attaches as an event-bus subscriber: the
moment *any* code path does `repository.create(principal, "interaction",
{...})` with `kind` in `{"email", "call", "sms"}`, the post-commit event fires
`_derive_contacts()`, which resolves/creates a person for `from_channel`/
`to_channels` and records participant rows. This is fully wired and tested,
but currently has no real-world trigger for `kind='email'` because nothing
yet creates email interactions from a polled mailbox.

## 4. Safety rules 4 & 5 — where implemented

**Rule 4 (transient failure must not lose data / cursor never advances past a
failed item):**
- `sync_poller.py:sync_account()`: cursor commit happens strictly *after* the
  per-contact loop completes without exception; any exception returns `False`
  immediately, leaving `next_cursor` uncommitted so the next poll cycle
  re-fetches the same batch from the old cursor.
- `contacts.py:fetch_contacts()` docstring states the contract explicitly:
  "Does NOT persist the cursor — the caller commits it only once every
  contact in the batch has been resolved."
- `queue.py:mark_failed()`: a failed job goes back to `pending` (reclaimable)
  with exponential backoff; only after `max_attempts` is exhausted does it
  move to `dead_letter` — still retained, never deleted, with `last_error`
  recorded.
- `queue.py:reclaim_stale_claims()`: a crashed worker's `claimed` row is reset
  to `pending` after 45 minutes rather than silently vanishing.

**Rule 5 (no per-process latches on recoverable external state):**
- `sync_poller.py` states it directly: a provider failure sets
  `status='error'` but the account is still polled every cycle afterward —
  `status` flips back to `'active'` the moment a sync succeeds. `poll_once()`
  explicitly includes `status in ("active", "error")` accounts, only skipping
  `pending`/`disabled`.
- `google_contacts.py:fetch_contacts()`: a Google 410 Gone (expired sync
  token) is caught and triggers an immediate full resync with an empty token
  rather than leaving the account permanently stuck.
- `microsoft_graph.py:mint_access_token()`: "A revoked/expired refresh token
  surfaces as GraphError; the caller decides how to record that (never a
  permanent latch — safety rule 5)."

## 5. Safety rule 7 — timezone-bounds computation

Implemented once, generically, in `server/providers/calendar.py:local_day_utc_window()`:
```python
def local_day_utc_window(account, date):
    tz_name, tz = account_timezone(account)
    local_start = datetime.fromisoformat(date).replace(tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    start = local_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = local_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return tz_name, start, end
```
`local_end` is computed via `timedelta(days=1)` wall-clock arithmetic on an
*aware* datetime, then separately converted to UTC — so a DST-transition day
naturally spans 23 or 25 real hours rather than a hardcoded 24. Both adapters
call this same function rather than reimplementing it. `microsoft_calendar.py`'s
own docstring explains why: Graph's `calendarView` start/end params are
always UTC regardless of the `Prefer: outlook.timezone` header, so a
hardcoded `Z` suffix on local midnight would silently shift the window by the
account's UTC offset.

## 6. OAuth linking

`server/core/account_link.py` is the dispatcher (not one of R3's six axes —
infra underneath contacts/calendar, same status as `http_retry.py`):
- `start_link(org_id, account_id, provider, redirect_uri)`: sweeps expired
  `core.oauth_pending` rows, generates a PKCE pair, dispatches by provider to
  `microsoft_oauth.start_link`/`google_oauth.start_link`.
- `complete_link(org_id, state, code, redirect_uri)`: peeks (non-destructively)
  at `oauth_pending` to learn the provider before dispatching (the adapter
  itself does the real single-use pop).
- **`microsoft_oauth.py`**: scopes `"offline_access User.Read
  Contacts.ReadWrite Calendars.ReadWrite"` (one consent screen for both).
  `complete_link()` pops pending state, exchanges code, saves
  `{refresh_token, scopes}`, calls `GET /me` for the email, activates the
  account. Any Graph-level failure sets account `status='error'` and
  re-raises as `LinkUpstreamError` (never lets `GraphError` escape).
- **`google_oauth.py`**: scopes include `contacts.readonly` + full `calendar`
  (read/write) + `userinfo.email`; requires `access_type=offline&prompt=consent`
  to reliably get a refresh token back on reconnect.
- **Refresh**: both transport adapters mint a fresh access token per call
  (never cached); if the provider returns a *new* refresh token, it's saved
  back immediately (Microsoft always rotates; Google usually doesn't).
- Credentials/cursors/links live in `server/core/accounts.py`, deliberately
  **not** a registered R4 entity (`account_credentials.payload` holds a
  refresh token; bypasses the generic repository's field-mask machinery
  entirely).

## 7. M5 pipeline: scheduling extraction → approval queue → calendar write

`server/core/scheduling_pipeline.py` subscribes to `interaction` CREATE
events, same subscriber pattern as `derivation.py`. `_on_interaction_created()`:
1. Filters to `kind == "email"` and `direction == "inbound"` only.
2. Calls `server/extraction/scheduling.py:extract_scheduling_info()` — an
   LLM-router `extract_structured()` call with a strict JSON schema and a
   validator that raises `ValueError` on any type mismatch (safety rule 6:
   `_validate_attendees()` explicitly rejects a bare string rather than
   `list()`-wrapping it, citing the literal one-attendee-per-character
   defect). Also strips quoted-reply text before extraction so a stale quoted
   proposal doesn't shadow a reply's real new time.
3. For each extracted event with a `date`, calls `proposals.create()` to
   insert a `proposed_change` row (`subject_type="interaction"`,
   `kind="calendar_event"`) — the only producer of calendar-event proposals
   today.
4. Routes the new proposal via `_route_proposal()`:
   - Master gate: `config.calendar_write_enabled()` — off means it stays
     `pending`, nothing else evaluated (safety rule 9).
   - `approval_mode` setting: `"auto"` always executes; `"confidence"`
     executes if `confidence >= confidence_threshold` (default 0.85).
   - Independently, `_auto_accept_reason()`: if the proposal's `category` is
     in `auto_accept_categories` **and** `trust.trust_reason()` returns
     non-None, it's additionally auto-accepted (additive-only).
   - Otherwise stays `pending`, best-effort-notifies via
     `channels_dispatch.send()` (swallows `ChannelError`).
5. Execution (`_execute_calendar_write()`) only ever happens via `_accept()`
   inside `_route_proposal()`: calls `calendar_dispatch.check_event()` to find
   an update target, builds `start_iso`/`end_iso`, and calls
   `calendar_dispatch.update_event()` or `.create_event()` against the
   **interaction owner's own** active connected account.
6. The actual queue-gate mechanics live in `server/core/proposals.py`:
   `approve()`/`decline()`/`auto_approve()` all funnel through
   `_set_decision()`, which re-reads the current row, raises
   `AlreadyDecidedError` if `status != 'pending'`, and otherwise does an
   optimistic-concurrency compare-and-swap so a proposal can only ever be
   decided exactly once.
