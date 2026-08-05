# API Reference

All routes are FastAPI, mounted from `server/api/app.py`. Errors from
`repository`/`registry`/`query`/`associations` (typed exceptions) are mapped
to HTTP status once, via exception handlers registered on the app — never
repeated per route.

## The generic records API — `server/api/records.py`

The one REST surface for every registered entity (R4) — core's and every
module's, with zero per-entity route code. `{entity}` is any name from
`02-data-model.md`'s tables.

| Method | Path | Purpose |
|---|---|---|
| GET | `/records` | Registry metadata — every entity this principal can at least read; drives the sidebar. |
| GET | `/records/{entity}/schema` | One entity's fields, custom fields, and (added this session) `profile_blocks`. |
| GET | `/records/{entity}` | List — querystring filter/sort/select. |
| POST | `/records/{entity}/query` | List — JSON body, for a filter tree too large/complex for a querystring. Re-permission-checked on every call (what a saved view replays through). |
| GET | `/records/{entity}/{id}` | One record. |
| POST | `/records/{entity}` | Create. |
| PATCH | `/records/{entity}/{id}` | Update (optimistic concurrency via `if_unmodified_since`). |
| DELETE | `/records/{entity}/{id}` | Delete. |
| GET | `/records/{entity}/{id}/related` | Associations, grouped and hydrated. |
| GET | `/records/{entity}/{id}/hierarchy` | The ancestor/descendant chain for one hierarchical association role. |
| GET | `/records/{entity}/{id}/children` | Records naming this one as parent through a real FK field. |
| GET | `/records/{entity}/{id}/timeline` | Children + (for a person) interactions, merged, newest-first. |
| GET | `/records/{entity}/roles` | Every association role `entity` can take part in, from either side — what the "+ Link" picker builds from. |
| POST | `/records/{entity}/aggregate` | Group-by + reduce — what every dashboard tile is built from (R4, no bespoke per-tile query). |
| POST | `/labels` | Resolve `{entity: [ids]}` → `{entity: {id: label}}`, for rendering a reference field as a name. |
| GET | `/records/dashboard-tiles?nav_group=` | Every `DashboardTile` registered for a vertical the principal can read (added this session, Phase 12). |
| GET | `/records/dashboard-groups` | Every distinct `nav_group` with at least one readable tile (added this session, Phase 12). |

## Associations — `server/api/records.py` (`association_router`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/associations` | Create a relationship. |
| POST | `/associations/{id}/end` | End one, dated, preserved (not deleted). |
| DELETE | `/associations/{id}` | Delete one outright. |

## Auth & first-run setup — `server/api/app.py` / `server/api/auth.py`

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness + RLS/privilege self-check (`pool.healthcheck()`). |
| GET | `/setup` | Whether first-run setup is still available. |
| POST | `/setup` | Create the org and its first administrator. Fails closed once an admin exists or `CRM_ALLOW_FIRST_RUN_SETUP=false`. |
| POST | `/auth/login` | Sets the session cookie. |
| POST | `/auth/logout` | Clears it. |
| GET | `/auth/me` | Current user/org/permissions — what `AuthContext.tsx` hydrates from. |
| GET | `/users` | (Admin) list. |
| GET | `/roles` | (Admin) permission roles. |

`server/api/auth.py` itself holds no routes — it's the shared dependency
layer (`current_session`, `current_principal`, `require_admin_principal`,
`guard_first_run`) every other router's routes depend on.

## Connected accounts (Microsoft/Google OAuth) — `server/api/accounts.py`

| Method | Path | Purpose |
|---|---|---|
| GET | `/accounts` | List the caller's own connected mail/calendar/contacts accounts. |
| POST | `/accounts/{provider}/link` | Start an OAuth link (redirect to provider consent). |
| GET | `/accounts/oauth/callback` | OAuth callback — completes the link. |
| DELETE | `/accounts/{account_id}` | Disconnect. |

## Connected messaging channels — `server/api/channels.py`

| Method | Path | Purpose |
|---|---|---|
| GET | `/channels` | List the caller's own linked Signal/Telegram identities. |
| POST | `/channels/link` | Link a Signal number or Telegram handle to the caller. |
| DELETE | `/channels/{channel_id}` | Unlink. |

Never accepts a target `user_id` — always scoped to the calling principal, no
admin override.

## Scheduling proposal decisions — `server/api/proposals.py`

| Method | Path | Purpose |
|---|---|---|
| POST | `/proposals/{proposal_id}/approve` | Approve a pending proposal (compare-and-swap; `AlreadyDecidedError` on a lost race). |
| POST | `/proposals/{proposal_id}/decline` | Decline one. |

Deliberately not the generic writable-fields path — every field on
`proposed_change` is `writable=False` there; these are the only legitimate
writers (safety rule 3's single execution choke point).

## Semantic search — `server/api/search.py`

| Method | Path | Purpose |
|---|---|---|
| POST | `/search` | Embeds the query text and searches `ai.embeddings`, scoped to the caller's own visibility (safety rule 10). |

## Settings — `server/api/settings.py`

| Method | Path | Purpose |
|---|---|---|
| GET | `/settings/{section}` | Read one settings section (`llm`/`approval`/`pipeline`/`compliance`). Admin-only. |
| PATCH | `/settings/{section}` | Read-merge-write update (safety rule 2). Admin-only. |
| GET | `/settings/llm/status` | Per-provider reachability, skipping a network call when the required secret isn't set. |
| POST | `/settings/llm/test` | Test unsaved form values without persisting them. |

## What's conspicuously absent

- **No mail API** — no send/list/sync-mail routes anywhere, matching the gap
  identified in `06-sync-and-jobs.md`.
- **No bulk/batch mutation endpoint** — every write is one record at a time
  through the generic path (bulk import, if it exists at all, would go
  through repeated `POST /records/{entity}` calls or a script talking to
  `repository` directly, not a dedicated endpoint).
- **No webhook receiver** — both messaging channels and (once built) any mail
  sync are poll-driven, not push/webhook-driven (see `05-messaging-channels.md`
  §5 and `06-sync-and-jobs.md`).
