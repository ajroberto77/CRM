import { useEffect, useState } from 'react'
import { apiGet, withQuery, ApiError } from '../lib/api'
import { buildSearchFilter } from './searchFilter'
import type { EntitySchema, ListResult, RecordRow } from './types'

/** Debounced live search against one entity's `searchable` fields (falling
 * back to `label_field`) -- built once here rather than duplicated per
 * caller. Shared by `RecordPicker` (a reference field's search-then-pick)
 * and available to `LinkRecordControl`'s equivalent search, which previously
 * had its own inline copy with no cancellation and no error surfaced (a
 * slow first keystroke's response could land after a faster later one and
 * show stale results).
 */
export function useRecordSearch(
  targetEntity: string | null,
  targetSchema: EntitySchema | null,
  query: string,
  limit = 10,
): { results: RecordRow[]; loading: boolean; error: string | null } {
  const [results, setResults] = useState<RecordRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!targetEntity || !targetSchema || !query.trim()) {
      setResults([])
      setError(null)
      setLoading(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    const handle = setTimeout(() => {
      const path = withQuery(`/records/${targetEntity}`, {
        filter: buildSearchFilter(targetSchema, query.trim()),
        limit,
      })
      apiGet<ListResult>(path)
        .then((r) => {
          if (!cancelled) setResults(r.records)
        })
        .catch((err) => {
          if (!cancelled) setError(err instanceof ApiError ? err.detail : String(err))
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }, 200)
    return () => {
      cancelled = true
      clearTimeout(handle)
    }
  }, [targetEntity, targetSchema, query, limit])

  return { results, loading, error }
}
