import { useCallback, useEffect, useState } from 'react'
import { apiDelete, apiPatch, apiPost, withQuery, apiGet, ApiError } from '../lib/api'
import type { FilterNode, ListResult, SavedView, SortSpec } from '../records/types'

/** Saved views are just the `saved_view` entity (server/core/registry.py) --
 * this hook is a thin wrapper over the generic records API, not a second
 * CRUD path (R4). */
export function useSavedViews(entity: string) {
  const [views, setViews] = useState<SavedView[]>([])
  const [loading, setLoading] = useState(true)
  const [canUseViews, setCanUseViews] = useState(true)

  const reload = useCallback(() => {
    setLoading(true)
    const path = withQuery('/records/saved_view', {
      filter: { field: 'entity', op: 'eq', value: entity },
      sort: [{ field: 'name', direction: 'asc' }],
    })
    return apiGet<ListResult>(path)
      .then((res) => {
        setViews(res.records as unknown as SavedView[])
        setCanUseViews(true)
      })
      .catch((err) => {
        // A principal with no grant on saved_view at all (the default for a
        // freshly-created role) must not break the page for entities they
        // CAN read -- views are a convenience, not a dependency of the list.
        if (err instanceof ApiError && err.status === 403) {
          setViews([])
          setCanUseViews(false)
          return
        }
        throw err
      })
      .finally(() => setLoading(false))
  }, [entity])

  useEffect(() => {
    reload()
  }, [reload])

  async function saveView(name: string, filters: FilterNode | null, sort: SortSpec[] | null, columns: string[] | null) {
    // Omit null optional fields rather than sending an explicit JSON null --
    // core.saved_views' filters/sort/columns columns are NOT NULL with a
    // DEFAULT, which only applies when the column is left out of the INSERT
    // entirely, not when the client sends `null` for it.
    const body: Record<string, unknown> = { entity, name, kind: 'table' }
    if (filters !== null) body.filters = filters
    if (sort !== null) body.sort = sort
    if (columns !== null) body.columns = columns
    await apiPost('/records/saved_view', body)
    await reload()
  }

  async function updateView(id: string, changes: Partial<Pick<SavedView, 'filters' | 'sort' | 'columns' | 'name'>>) {
    await apiPatch(`/records/saved_view/${id}`, { changes })
    await reload()
  }

  async function deleteView(id: string) {
    await apiDelete(`/records/saved_view/${id}`)
    await reload()
  }

  return { views, loading, canUseViews, saveView, updateView, deleteView }
}
