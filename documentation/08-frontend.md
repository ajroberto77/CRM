# Frontend — `web/src/`

The frontend is React + TypeScript + Vite, deliberately near-dependency-free
(`react`, `react-dom`, `react-router-dom` and nothing else beyond dev
tooling). This file covers everything except `web/src/records/` (RecordPage,
EntityListPage, RecordTable/RecordBoard, ProfileBlocks, saved-view hooks),
`web/src/app/Shell.tsx`/`HomePage.tsx`/`VerticalDashboard.tsx`, and
`web/src/modules/funds/` — those are covered in `01-architecture.md` and
`02-data-model.md`'s frontend-facing sections, having been built/reviewed
directly in this session.

## 1. `web/src/auth/` — authentication & first-run setup

**`AuthContext.tsx`** — `AuthProvider`/`useAuth()`. On mount, calls `GET
/setup` to check `first_run_required`; if true, sets `status: 'anonymous'`
with `firstRunRequired: true` (routes to `SetupPage`). Otherwise calls `GET
/auth/me`, storing `user`, `org`, and a per-entity permissions map
(`can_create`/`read_level`/`edit_level`/`delete_level`/`field_masks`) from the
response. A 401 on `/auth/me` is treated as `anonymous` (routes to
`LoginPage`); any other error rethrows. Exposes `login()` → `POST
/auth/login` then refresh; `logout()` → `POST /auth/logout` + local reset;
`setup()` → `POST /setup` then refresh.

**`LoginPage.tsx`** — plain email/password form. **`SetupPage.tsx`** —
org name / admin name / email / password form, creates the org and its first
administrator.

**Session mechanics end to end**: login/setup responses set an httpOnly,
`SameSite=lax`, `Secure`-unless-disabled cookie. The frontend never touches
the token directly — `lib/api.ts`'s `apiFetch` sends every request with
`credentials: 'include'`, and Vite's dev proxy keeps frontend/backend
same-origin. Server-side, `current_session` reads the cookie first, falling
back to an `Authorization: Bearer` header (non-browser callers/workers);
`first_run_required()`/`guard_first_run()` gate `/setup` by counting
identities and fail closed once configured or disabled.

## 2. `web/src/settings/` — admin/user settings pages

- **`SettingsShell.tsx`** (`/admin/settings`) — layout shell; left nav lists
  LLM (admin-only), Connected Accounts, Connected Channels, then every
  registered entity with `nav === 'settings'`.
- **`LlmSettingsPage.tsx`** (`/admin/settings/llm`, admin-gated) —
  configures the org's LLM settings as a singleton: active provider + model,
  Ollama host/port/MAC (Wake-on-LAN), a live status dot per provider, a "Test
  connection" button per provider, and an ordered fallback chain. API keys
  are explicitly not edited here — only shown as "comes from `<ENV_VAR>`"
  hints.
- **`ConnectedAccountsPage.tsx`** (personal, not admin-gated) — lists
  Microsoft 365/Google account links with a status dot; "Connect" does a real
  browser redirect to the provider's OAuth consent screen (not a fetch-based
  flow).
- **`ConnectedChannelsPage.tsx`** (also personal) — links a Signal number or
  Telegram handle so a user can reply "yes"/"no" to a proposal notification
  directly from that messaging app.
- **`SettingsEntityListPage.tsx`** (`/admin/settings/:entity`) — thin wrapper
  rendering `EntityListPage`, so entities can be mounted under the settings
  shell without `EntityListPage` caring where it's mounted.

## 3. `web/src/proposals/` — scheduling proposal approval queue

**`PendingProposalsPage.tsx`** (`/review/proposals`, "Scheduling Suggestions")
— fetches `proposed_change` records (`status = pending`, sorted newest
first). Each card shows the extracted payload, category, and confidence.
"Approve"/"Decline" call `POST /proposals/{id}/approve`/`/decline` — a
dedicated endpoint, not the generic writable-fields REST path, since every
field on `proposed_change` is `writable=False` there. On success the card is
removed locally; on failure (e.g. `AlreadyDecidedError` from a concurrent
decision) it re-fetches to show the real state — directly exercising the
compare-and-swap design in `server/core/proposals.py`.

## 4. `web/src/command/` — command palette

**`CommandPalette.tsx`** — mounted once in `Shell.tsx`, listens globally for
Cmd/Ctrl+K (toggle) and Escape (close). Matches the typed query against the
sidebar's entity list first (substring match); if nothing matches and the
query is ≥3 chars, debounces (250ms) and falls back to semantic search via
`POST /search`, rendering each hit's owner type + a snippet. No `cmdk`/`kbar`
dependency — reuses the existing modal-overlay styling.

## 5. `web/src/lib/` — shared utilities

- **`api.ts`** — the single fetch wrapper (`apiGet`/`apiPost`/`apiPatch`/
  `apiDelete` + `withQuery`); always `credentials: 'include'`; normalizes
  FastAPI's `detail` (string or Pydantic validation-error array) into one
  error string.
- **`format.ts`** — the one per-field-kind value formatter (`formatValue`),
  plus `formatDate`/`formatDateTime`/`formatCurrency`, `recordLabel`
  (best-effort record display label with a fallback field list),
  `formatDateRange` (association date-range phrasing), `fieldLabel`.
- **`navLinkClass.ts`** — one factory producing the active/inactive NavLink
  className toggler, shared by the sidebar and settings sub-nav.

## 6. `web/src/app/App.tsx` — full route table (current)

```
/                                index → HomePage
/e/:entity                       EntityListPage
/e/:entity/:recordId             EntityListPage (split-panel table+detail)
/r/:entity/:recordId             RecordPage (dedicated full-page record view)
/dashboard/:navGroup             VerticalDashboard
/admin/settings                  SettingsShell
  (index)                        → redirect to connected-accounts
  /admin/settings/llm             LlmSettingsPage
  /admin/settings/connected-accounts   ConnectedAccountsPage
  /admin/settings/connected-channels   ConnectedChannelsPage
  /admin/settings/:entity          SettingsEntityListPage (dynamic per-entity)
/review/proposals                PendingProposalsPage
*                                 redirect to /
```
All authenticated routes render inside `<Shell>`. Route path choices
deliberately avoid every prefix the Vite dev proxy forwards straight to the
backend (`/settings/*`, `/proposals/*`) — hence `admin/settings` and
`review/proposals` rather than the more obvious paths, so a hard reload/deep
link doesn't hit the API instead of the SPA.

## 7. `web/styles/tokens.css` — design tokens

Single file, the only file in the repo permitted a literal hex/shadow/font
value. Structure:
- **Primitives** (`--bg0..3`, `--navy`, `--text0..3`, accent colors, font
  stacks) — carried over from a sibling product ("Cal") for visual family
  consistency.
- **Semantic layer** — surfaces (`--surface-base/sunken/raised/strong/chrome/
  overlay`), text (`--text-primary/secondary/muted/subtle/...`),
  accents/status, tinted fills (`--tint-*-bg/border`, a consistent
  10%-opacity-fill/30%-opacity-border recipe so no component hand-writes
  `rgba()`).
- **Elevation** (`--shadow-card`, `--shadow-modal`), **geometry**
  (`--radius-sm/md/lg/pill`, `--titlebar-h`, `--sidebar-w`, `--content-pad`),
  **type scale** (`--fs-micro` through `--fs-title`), and a **density** block
  overridden by `data-density="compact"`.
- **Dark mode**: only the semantic layer is remapped, never primitives or
  component CSS directly. Two paths — a `prefers-color-scheme: dark` media
  query scoped to `:root:not([data-theme="light"])` (system default) and
  `:root[data-theme="dark"]` (explicit user override) — with identical
  values, so an explicit `data-theme` wins over system preference either way.

## 8. Other top-level items

- **`web/src/main.tsx`** — mounts `<App>` inside
  `<StrictMode><BrowserRouter><AuthProvider>`, imports `styles/tokens.css` and
  `app/global.css`, and imports `./modules` (side-effect only) to register
  every installed module's frontend.
- **`web/src/modules/index.ts`** — `import.meta.glob('./*/index.ts', {
  eager: true })`; the browser-side counterpart to
  `server/core/modules.py`'s `install_enabled_modules()`. Adding a module's
  frontend registration means adding a new `web/src/modules/<name>/`
  directory — no edit to this file or `main.tsx`, and core code is never
  allowed to name a module directly.
- **`web/src/app/global.css`** — app-level component styles, as opposed to
  `tokens.css`'s pure variables.
