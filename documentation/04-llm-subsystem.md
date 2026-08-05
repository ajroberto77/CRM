# LLM Subsystem

## 1. `server/llm/` directory structure

| File | Role |
|---|---|
| `server/llm/router.py` | The single LLM-axis dispatcher (R3). Defines `LLMError`/`ProviderUnavailable`, the adapter contract, chain resolution, `call_llm()`, `extract_structured()`, `list_models()`, status/test-connection helpers. |
| `server/llm/ollama_llm.py` | Local/self-hosted adapter; also implements Wake-on-LAN. |
| `server/llm/openai_llm.py` | OpenAI Chat Completions adapter. |
| `server/llm/anthropic_llm.py` | Anthropic Messages API adapter (direct API, not Claude Code). |
| `server/llm/gemini_llm.py` | Google Gemini `generateContent` adapter. |
| `server/llm/claudecode_llm.py` | Adapter for a self-hosted "claude-api-bridge" (async ask/poll HTTP service), distinct from the direct Anthropic adapter. |
| `server/llm/embeddings.py` | Separate embeddings API (`embed()`), dispatches only to ollama/openai. |

Companion files: `server/extraction/scheduling.py` (the one extraction task
built on the router), `server/core/embeddings.py` +
`server/core/interaction_embeddings.py` (storage/event-wiring for
`ai.embeddings`), `server/jobs/embed_interaction.py` (job handler),
`server/api/search.py` (semantic search route), `server/api/settings.py`
(LLM settings/status/test routes).

## 2. The router — `server/llm/router.py`

**Adapter contract**: every adapter implements `call(prompt, *, model,
settings, want_json=False, temperature=None, max_tokens=None) -> str`,
`call_structured(prompt, schema, *, model, settings, ...) -> Any`, and
`list_models(settings) -> list[str]`. `settings` is always the org's whole
`"llm"` settings section, not a bespoke arg list, since Ollama needs
`ollama_host`/`ollama_port` while others don't.

**Provider identity is only branched on in `_import_adapter()`**, an
`if/elif` over `PROVIDERS = ("ollama", "openai", "anthropic", "gemini",
"claudecode")`. No other file may branch on LLM provider identity (R3).

**Selection/configuration per org** — `server/core/settings.py`, section
`"llm"` (a JSONB blob per org, not env vars):
- `resolved_chain(org_id)`: reads `section["chain"]` (an ordered list of
  `{provider, model}`); if empty, synthesizes a one-entry chain from
  `section["provider"]` (default `"ollama"`) and `section[f"{provider}_model"]`.
- `call_llm()` is the non-chain, single-provider path: calls the org's one
  active provider once and lets any failure propagate. Used for one-off prose
  calls, not extraction.
- API keys remain infra-level (`server/config.py`'s `_SECRET_ENV`), read
  fresh per call.

**Fallback chain logic** — `extract_structured()` and its helper
`_extract_with_repair()`:
- Only `ProviderUnavailable` rolls the chain to the next `{provider, model}`
  entry; every other exception type is not retried across providers.
- `classify_http_error()` is the single translation point every adapter's
  HTTP calls funnel through: no response at all, or status in `{401, 402,
  403, 408, 429}`, or any 5xx → `ProviderUnavailable`; anything else (e.g. a
  400 — a malformed request the platform itself sent) → plain `LLMError`
  (never retried elsewhere).
- Malformed/unparseable output (`LLMError`, `ValueError`,
  `json.JSONDecodeError`) triggers exactly **one same-provider repair
  retry**, appending a hint describing what was wrong to the prompt. It never
  cascades to the next provider — cascading on a quality failure would burn
  every configured provider on one genuinely ambiguous input.
- If every provider in the chain raises `ProviderUnavailable`,
  `extract_structured()` raises `LLMError` with all skip reasons joined.

Other functions: `list_models(org_id, provider)` for the settings UI's
dropdown; `check_llm_status()` (iterates all 5 providers, skipping a network
call if the required secret isn't set); `test_llm_connection()` (fires one
call against unsaved form overrides, never touching `core.settings`).

## 3. Per-provider adapters

- **Ollama** — no auth. `call_structured()` sends the literal JSON Schema as
  Ollama's `format` field (real grammar-constrained decoding). Tunes
  `num_ctx`/`num_predict`; `keep_alive` keeps the model resident. Implements
  **Wake-on-LAN**: sends a magic packet if `ollama_mac` is set and the host
  isn't reachable, waits up to `ollama_wake_timeout` (default 90s), raises
  `ProviderUnavailable` on timeout. A per-process, per-MAC cooldown throttles
  repeat wake attempts without permanently latching the host as dead (safety
  rule 5).
- **OpenAI** — Chat Completions API. `call_structured()` uses strict-mode
  JSON Schema (`response_format: {"type": "json_schema", ...,
  "strict": true}`). `list_models()` filters to `gpt-`/`o1`/`o3`/`o4` prefixes.
- **Anthropic** (direct API) — no native JSON-schema response mode;
  `call_structured()` uses forced tool-use (`tool_choice: {"type": "tool",
  "name": "extract"}`, schema as the tool's input schema). Raises plain
  `ValueError` if no tool-use block is present (a quality failure, correctly
  not `ProviderUnavailable`).
- **Gemini** — the one adapter authenticating via URL query param (`?key=...`)
  rather than a header. `call_structured()` sets `responseMimeType:
  "application/json"` + `responseSchema` inside `generationConfig`.
- **Claude Code** (bridge adapter) — talks to a self-hosted "claude-api-bridge,"
  distinct from the direct Anthropic adapter. Async request/poll (`POST
  /api/ask` → `requestId`, then polled every 2s up to 300s; timeout raises
  `ProviderUnavailable`). A missing key is deliberately NOT treated as
  unavailable (unlike the other three keyed providers) since the bridge
  expects the header present regardless of value. **No native
  structured-output primitive**: the schema is embedded as prompt text and a
  regex extracts the first JSON object from free text, raising `ValueError`
  if none is found. `list_models()` returns a hardcoded alias list
  (`opus`/`sonnet`/`haiku`/`fable`) since there's no listing endpoint.

## 4. `server/extraction/` — task-agnostic extraction

Only one task file today: `server/extraction/scheduling.py`, ported from
Cal. It owns its own prompt, JSON Schema, and field validators; the router
never knows about scheduling, dates, or attendees — a second extraction task
gets its own new file here, never a branch inside `router.py`.
`extract_scheduling_info(org_id, email_text, *, reference_date=None)` strips
quoted-reply text (earliest match wins across several quote-marker patterns,
so a reply's genuinely new time isn't shadowed by stale quoted content) and
calls `llm_router.extract_structured(org_id, prompt, EVENT_EXTRACTION_SCHEMA,
validate=_validate_extraction)`.

Consumer: `server/core/scheduling_pipeline.py` (see `06-sync-and-jobs.md`
§7 for the full pipeline).

## 5. Safety rule 1 enforcement — "No LLM in any action path"

`server/channels/commands.py`'s `dispatch_command()` is pure exact-match code
end to end. See `05-messaging-channels.md` §2 for the full command grammar.

## 6. Safety rule 6 enforcement — repair-retry via ValueError

Two layers:
- **Generic mechanism**: `router.py`'s `_extract_with_repair()` catches
  `(LLMError, ValueError, json.JSONDecodeError)` from `validate(obj)` and
  retries once on the same provider with a repair hint.
- **Concrete validators**: `scheduling.py`'s per-field validators —
  `_validate_date` (uses `date.fromisoformat`, not regex alone, to reject fake
  dates like `"2026-13-45"`), `_validate_time`, `_validate_duration`,
  `_validate_category` (allows `None`, ties to safety rule 8),
  `_validate_confidence`. The canonical bug this guards against:
  `_validate_attendees()` explicitly **rejects a bare string rather than
  `list()`-wrapping it** — `list("a@b.com")` would explode into one attendee
  per character, each becoming a real calendar invitee. This is verbatim the
  incident safety rule 6 documents.

## 7. Embeddings / semantic search (`ai` schema, pgvector)

**Schema**: `EMBEDDING_DIM = 768` (matches Ollama's `nomic-embed-text`
default; OpenAI's call requests `dimensions=768` to truncate to match). Table
`ai.embeddings`: `org_id`, `owner_type`, `owner_id`, `visibility_user_id`
(nullable), `chunk_kind`, `chunk_index`, `content_hash`, `content_text`,
`model`, `dim`, `embedding vector(768)`. Unique index on `(org_id, owner_type,
owner_id, chunk_kind, content_hash)` (idempotent re-embed/upsert); HNSW cosine
index for retrieval.

**Usage**: semantic search over interaction bodies. Pipeline: `interaction`
CREATE event → `interaction_embeddings.py` enqueues an `embed_interaction` job
(not inline, so a slow LLM call never blocks the write) → the job handler
chunks the body (fixed 2000-char windows), embeds each chunk, stores via
`store_chunk()`. Query path: `POST /search` embeds the query text and calls
`embeddings.search()`.

**Safety rule 10 enforcement**:
- Write side: the job handler sets `visibility_user_id=owner_id` (the
  interaction's own owner) on every stored chunk — body-derived content is
  owner-scoped, not org-wide.
- Read side: `search()` is the sole gate —
  `WHERE org_id = %s AND (visibility_user_id IS NULL OR visibility_user_id =
  %s) ORDER BY embedding <=> %s::vector LIMIT %s`. A row is only returned when
  it's org-wide or belongs to the calling user. `ai.embeddings` is
  deliberately not a registered R4 entity — enforcing this at the generic
  own/team CRUD field-mask model was rejected in favor of one hand-written
  choke point.

## 8. Config

**`server/config.py`** — infrastructure-only, read once at import:
- `_SECRET_ENV` maps logical secret names to env vars for all 4 keyed LLM
  providers (`anthropic_api_key`, `openai_api_key`, `gemini_api_key`,
  `claudecode_key`). Ollama has no secret.
- `get_secret(name)` returns `''` if unset; raises `KeyError` for an unknown
  name.
- `calendar_write_enabled()` is the master live-action gate consumed by the
  scheduling pipeline (safety rule 9).

**Per-org LLM configuration** lives entirely in `server/core/settings.py`,
not `config.py`. `SECTIONS = ("llm", "approval", "pipeline", "compliance")`.
`update_settings()` performs the read-merge-write inside one `FOR
UPDATE`-locked transaction — the mechanism safety rule 2 requires.

Expected `"llm"` section keys (inferred from adapter usage): `provider`,
`chain`, `{provider}_model` per provider, `temperature`, plus Ollama-specific
`ollama_host`/`ollama_port`/`ollama_mac`/`ollama_wol_broadcast`/
`ollama_wake_timeout`/`ollama_num_ctx`/`ollama_num_predict`/`ollama_keep_alive`/
`ollama_embedding_model`; Claude Code's `claudecode_host`/`claudecode_port`;
`openai_embedding_model`.

**HTTP layer**: `server/api/settings.py` — generic `GET/PATCH
/settings/{section}` (admin-only), plus `GET /settings/llm/status` and `POST
/settings/llm/test`.
