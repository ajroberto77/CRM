import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { apiDelete, apiPatch, apiPost, withQuery, apiGet, ApiError } from '../lib/api'
import type { FilterNode, ListResult, SavedView, SortSpec } from '../records/types'

interface ViewsState {
  views: SavedView[]
  loading: boolean
  canUseViews: boolean
}

const EMPTY_STATE: ViewsState = { views: [], loading: true, canUseViews: true }

/** Module-level cache + subscriber list, keyed by entity -- shared by every
 * `useSavedViews(entity)` call in the app rather than each holding its own
 * `useState`. Two independent callers exist per entity in steady state
 * (Shell.tsx's sidebar nav item, and that entity's own EntityListPage), and
 * without a shared store: (1) both fire their own GET on mount -- a dozen+
 * primary entities means a dozen+ parallel requests just to populate nav
 * links most of which are never opened -- and (2) saving/renaming/deleting a
 * view through the in-page ViewSwitcher only ever refreshed the page's own
 * hook instance, so the sidebar's copy went stale for the rest of the
 * session. `useEntitySchema.ts`'s cache solves problem (1) for read-only
 * data; this adds the subscriber-broadcast half needed because saved views
 * are mutated live within a session, unlike a schema. */
const store: Record<string, ViewsState> = {}
const listeners: Record<string, Set<() => void>> = {}
const inFlight: Record<string, Promise<void> | undefined> = {}

function getState(entity: string): ViewsState {
  return store[entity] ?? EMPTY_STATE
}

function setState(entity: string, next: ViewsState) {
  store[entity] = next
  listeners[entity]?.forEach((listener) => listener())
}

function subscribe(entity: string, onChange: () => void) {
  if (!listeners[entity]) listeners[entity] = new Set()
  listeners[entity].add(onChange)
  return () => {
    listeners[entity]?.delete(onChange)
  }
}

/** Fetch (or re-fetch) `entity`'s views and broadcast the result to every
 * subscribed component. Callers dedupe against `inFlight` so a mount storm
 * (every sidebar item mounting at once) still issues one request per entity,
 * not one per mounted component. */
function load(entity: string): Promise<void> {
  if (inFlight[entity]) return inFlight[entity]

  const path = withQuery('/records/saved_view', {
    filter: { field: 'entity', op: 'eq', value: entity },
    sort: [{ field: 'name', direction: 'asc' }],
  })
  const promise = apiGet<ListResult>(path)
    .then((res) => {
      setState(entity, {
        views: res.records as unknown as SavedView[], loading: false, canUseViews: true,
      })
    })
    .catch((err) => {
      // A principal with no grant on saved_view at all (the default for a
      // freshly-created role) must not break the page for entities they
      // CAN read -- views are a convenience, not a dependency of the list.
      if (err instanceof ApiError && err.status === 403) {
        setState(entity, { views: [], loading: false, canUseViews: false })
        return
      }
      setState(entity, { ...getState(entity), loading: false })
      throw err
    })
    .finally(() => {
      delete inFlight[entity]
    })
  inFlight[entity] = promise
  return promise
}

/** Saved views are just the `saved_view` entity (server/core/registry.py) --
 * this hook is a thin wrapper over the generic records API, not a second
 * CRUD path (R4). */
export function useSavedViews(entity: string) {
  const state = useSyncExternalStore(
    useCallback((onChange) => subscribe(entity, onChange), [entity]),
    () => getState(entity),
  )

  useEffect(() => {
    // Already cached (a sibling component for this entity loaded first) or
    // already loading -- `load()`'s own `inFlight` guard would no-op anyway,
    // but skipping the call entirely avoids a redundant module-level lookup
    // on every one of a dozen+ simultaneous mounts.
    if (store[entity] || inFlight[entity]) return
    load(entity)
  }, [entity])

  async function saveView(
    name: string,
    filters: FilterNode | null,
    sort: SortSpec[] | null,
    columns: string[] | null,
    kind: SavedView['kind'] = 'table',
    groupBy: string | null = null,
  ) {
    // Omit null optional fields rather than sending an explicit JSON null --
    // core.saved_views' filters/sort/columns/group_by columns are NOT NULL
    // with a DEFAULT, which only applies when the column is left out of the
    // INSERT entirely, not when the client sends `null` for it.
    const body: Record<string, unknown> = { entity, name, kind }
    if (filters !== null) body.filters = filters
    if (sort !== null) body.sort = sort
    if (columns !== null) body.columns = columns
    if (groupBy !== null) body.group_by = groupBy
    await apiPost('/records/saved_view', body)
    await load(entity)
  }

  async function updateView(
    id: string,
    changes: Partial<Pick<SavedView, 'filters' | 'sort' | 'columns' | 'name' | 'kind' | 'group_by'>>,
  ) {
    await apiPatch(`/records/saved_view/${id}`, { changes })
    await load(entity)
  }

  async function deleteView(id: string) {
    await apiDelete(`/records/saved_view/${id}`)
    await load(entity)
  }

  return {
    views: state.views,
    loading: state.loading,
    canUseViews: state.canUseViews,
    saveView,
    updateView,
    deleteView,
  }
}
