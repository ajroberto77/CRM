# Source patterns — what comes from where

`Cal`, `JA` and `CATO` are **read-only reference repositories**. Nothing is
imported, submoduled, or shared. Patterns are ported into this codebase and then
owned here.

Read each at the correct ref. `main` is stale or empty in two of the three, and
reading it would reproduce a design built on the wrong code.

| Repo | Ref | Path |
|---|---|---|
| Cal | `claude/calendar-coordination-office365-9n6jq3` | repo root |
| JA | `claude/db-modularization` | `prototype/` |
| CATO | default branch | `src/python/` |

## From Cal

Cal is a calendar-coordination agent: it reads mail across linked accounts, uses
an LLM to extract scheduling content, checks the calendar, and creates events.
Its office365 branch carries 288 tests and a live-deployment audit.

| Source | What transfers | Change on port |
|---|---|---|
| `extract.py` | Task-agnostic LLM router: `call_llm`, `extract_structured`, `list_models`, `check_llm_status`, `test_llm_connection`; the **provider fallback chain** and `ProviderUnavailable` | Config moves from `.env` + `config.json` to Postgres-backed settings; secrets stay in env |
| `scheduling_extraction.py` | Task owns its own prompt + schema + validation; the router stays task-agnostic | Becomes `extraction/`, one file per task |
| `http_retry.py` | Shared retry/backoff mechanics | Direct port |
| `mail_provider.py`, `calendar_provider.py`, `account_link.py` | The dispatch contract — generic name, one `elif` per provider | Add a fourth axis: contacts |
| `microsoft_graph_client.py` | PKCE public client, `/common` tenant, 429/503/504 retry honoring `Retry-After`, refresh-token rotation, provider-agnostic credential storage, `request_mime` | Direct port |
| `microsoft_calendar.py` | `calendarView` local-day→UTC window, `Prefer: outlook.timezone`, create-vs-update heuristic, PATCH not PUT | Direct port |
| `senders.py`, `trusted_senders.py` | One `is_trusted()`/`trust_reason()` over people **and** services; public-domain refusal; normalize-at-write | Becomes `core/trust.py`; generalized past calendar |
| `dashboard_auth.py` | `pbkdf2_sha256$<iterations>$<salt>$<hash>` self-describing format, `compare_digest`, both halves always evaluated, malformed hash denies | Port hashing; replace HTTP Basic with sessions; **fail closed**, not open |
| `db.py` | Table dicts as single source of truth, idempotent `CREATE` + `ADD COLUMN` | Re-expressed for Postgres |
| `settings.py` | Hot-reloadable validated config, read-merge-write, explicit clearable keys | Moves to Postgres |
| `accounts.py` `get_acting_account()` | `per_account` vs `central` send identity | Direct port; matters more with many users |
| `pipeline.py` | poll → classify → extract → route → approve → act → confirm; approval modes; auto-accept rule | Approval queue generalizes to gate every AI and sync write |
| `tests/`, `deploy/` | pytest conftest isolation, mocked HTTP; systemd units | Extend |

### Cal's six live-deployment defects → `CLAUDE.md` safety rules

Each was found by auditing a running deployment, reproduced before being fixed,
and given a regression test verified to fail against the original code.

1. **Config destruction.** `set_public_config()` started from a loader returning
   only the 8 generic keys and wrote that back as the whole config, destroying
   all 15 LLM keys on every Settings save. Invisible while running, because
   reload only ever *set* env vars and never cleared them — it surfaced after the
   next restart as a silent revert to defaults. → *read-merge-write, plus an
   explicit clearable-key set.*
2. **Gate bypass.** The Approve button bypassed `calendar_write_enabled`
   entirely and wrote to real calendars while writes were disabled. The fix put
   the check in the single execution function, covering every caller *by
   construction*, and made it **raise** — a no-op would let the caller then mark
   the event scheduled. → *gates at the choke point, raising.*
3. **Permanent data loss on a transient outage.** Extraction failure marked the
   message `needs_review` and returned normally, so the poller committed its
   delta cursor and the message left the provider's stream for good. → *retain
   bodies; reprocess path restricted to failed items so a retry cannot
   duplicate.*
4. **Stuck latch.** `_ensure_awake` latched a per-process flag and returned early
   forever, so a host that slept mid-day was never woken again. → *no per-process
   latches on recoverable external state.*
5. **Unvalidated types reaching a provider.** `_validate_extraction` checked only
   date and time. `duration_minutes="90"` raised deep in `timedelta`;
   `"2026-13-45"` passed the format regex; and `attendees="alice@example.com"`
   was exploded by `list()` into **one attendee per character, each sent to Graph
   as a required attendee on a real meeting invite**. → *validate every field,
   including types, at the layer where `ValueError` triggers the repair-retry.*
6. **Timezone window shift.** `check_event` hardcoded `Z` on the `calendarView`
   bounds, making the window the UTC day rather than the account's. **`Prefer:
   outlook.timezone` only affects how Graph renders times in the response — it
   does not reinterpret query params.** For New York the window shifted 4–5h,
   missing events after 8pm local and pulling in the previous evening's, feeding
   the wrong day to the create-vs-update heuristic. → *compute bounds; local
   midnight + one day is wall-clock arithmetic, so DST days span 23 or 25 hours.*

### Still unverified in Cal — do not assume it works

Graph's event `start/end.timeZone` defaults to expecting **Windows** zone names
("Eastern Standard Time"), not the IANA names used everywhere else. Cal sends
`Prefer: outlook.timezone="<IANA>"` to work around this **but has never
exercised it against a live account.** Verify a created event lands at the
correct wall-clock time before enabling any unattended calendar write.

### A UI behavior Cal had to fix three times

Controls backed by a live provider call must not (a) rebuild and wipe
live-fetched state on every page visit, (b) reset a verified selection after
save, or (c) display an unverified saved value as though confirmed. Any CRM
control fed by a provider call inherits this rule.

## From JA

JA is a co-parenting communication assistant. It is the only sibling with a
**working two-way messaging control loop** — Cal's `signal_notifier.py` is a stub
that raises `NotSupportedError`.

| Source | What transfers | Change on port |
|---|---|---|
| `messaging.py` | The messaging abstraction: `send(text, thread_to=)`, `receive_commands() -> [(text, quoted_id)]`, a `_PROVIDERS` registry, **opaque message ids** the caller stores and never interprets | Becomes `channels/dispatch.py`; Telegram is a second registry entry |
| `messaging.py` | signal-cli invocation shape: `-u` and `-o json` are **global** flags preceding the subcommand; quote-reply threading via `--quote-timestamp`/`--quote-author` | Direct port |
| `messaging.py` | Rate-gated `libsignal*` temp-dir cleanup | Direct port |
| `reply_loop.py` | The command dispatch and its safety rules (below) | Owner check becomes a `contact_channels` → `users` lookup |
| `classify.py` | Same five providers as Cal's `extract.py` (ollama/openai/anthropic/gemini/claudecode), confirming the shape -- but **no cross-provider fallback chain and no tests**. Neither `_call_openai_compatible()` nor `_prime_ollama()` exists in JA or Cal (a prior inaccuracy in this doc); M3 (`server/llm/`) ported from **Cal's `extract.py`**, the one with a real, tested `ProviderUnavailable`-driven fallback chain | Not ported from here -- see Cal's row above |
| `state_db.py` | Migration pattern (table dicts, `ALTER TABLE ADD COLUMN` if missing) — Cal ported this from here | Re-expressed for Postgres |
| `contacts_db.py` | Free-text roles; **`aliases`** so an LLM can resolve "her"/"mom"/"the mediator" to real people | Aliases generalize to entity resolution for LLM context |

### `reply_loop.py`'s safety rules — requirements, not suggestions

- **No LLM anywhere in the action path.** Commands are exact-match vocabulary.
  From the module's own docstring: a real send "can only ever be triggered by
  exact-match code, never by anything resembling judgment." JA tried the agentic
  version first — binding an LLM agent to the Signal channel to dispatch via a
  skill — and it failed in practice, because the model did not reliably choose to
  invoke the skill (a plain "test" message got a chatty reply instead).
- **A quote that does not resolve stops cold.** It must **not** fall back to
  most-recent-pending. Found live: quote-replying to a non-actionable
  notification with a conversational answer ("I do, especially if we go to the
  beach after!") was redirected into an unrelated stale pending item **and sent
  as a real email**. A deliberate quote of a non-actionable message is strong
  evidence the sender is not approving anything; guessing is actively dangerous.
- **Compare-and-swap claim before acting**, reverting to the *original* status on
  failure so the item stays retryable. Without it, a crash between a successful
  send and the commit leaves the row claimable and a retry resends.
- **Failures must reach the user on the channel.** A narrower `except` once let a
  genuine SMTP error vanish into the daemon's catch-all, leaving the user with no
  idea their approval had failed.
- **`signal-cli` leaks ~167MB per invocation.** Version 0.14.5 extracts a
  randomly-named `libsignal*` temp dir on every call and never cleans up. Under a
  burst of notifications or weeks of a long-running poller these accumulate and
  exhaust `/tmp`, after which *every* signal-cli call fails with "Disk quota
  exceeded." This took down Signal notifications entirely during JA's own
  testing. Cleanup ships inside the adapter, not as a host-level ops step.

## From CATO

CATO is a desktop research platform on ten PostgreSQL databases.

| Source | What transfers |
|---|---|
| `cato_config.py` | Config class + module-level singleton + public getter functions; every setting reads an env var with a default. Adding a value = one class attribute + one getter. |
| `normalize_cik()` | The discipline, not the function: **one normalizer per value type, imported everywhere, never reimplemented, applied at every insertion point.** Becomes `core/identity.py`. |
| psycopg2 usage | Explicit cursor pattern, no wrapper abstractions, `execute_batch` for bulk inserts. |
| `cato_coordinator` | `workers` registry + `work_queue` tables kept in a database separate from domain output, which is what lets one worker table span unrelated pipelines. Becomes the `jobs` schema. |
| `isKnownDatabase()` | The lesson: a hardcoded name list went stale and every query silently failed with "Unknown database". Prefer convention checks over enumerations. |
| `.claude/agents/` | Subagent-enforced project conventions. `ui-design-reviewer` in particular documents a real incident where unscoped CSS class names collided across stylesheets and silently clobbered each other — hence this repo's class-prefix rule. |

## What is deliberately **not** ported

- **Cal's fail-open-when-unconfigured auth.** It exists so pulling the change
  cannot lock a single operator out of the Settings page needed to configure it.
  A multi-user CRM fails closed, with a first-run setup path instead.
- **JA's single-owner gate.** `OPENCLAW_OWNER_NUMBER` gates every command
  against one phone number. Here the sender resolves through `contact_channels`
  to a user; an unrecognized sender is ignored exactly as JA ignores a non-owner.
- **CATO's multi-database layout.** Ten databases exist because CATO has ten
  unrelated domains and accepts JS-side cross-database joins. This is one domain:
  one database, logical schemas, real joins.
- **SQLite and its WAL/threading model.** Both Cal and JA use SQLite; this is
  Postgres from the start.
