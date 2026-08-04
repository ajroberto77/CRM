import { useEffect, useState } from 'react'
import { apiGet, withQuery, ApiError } from '../lib/api'
import type { FilterNode, ListResult, SortSpec } from './types'

/**
 * The one fetch for a filtered/sorted page of an entity's records --
 * extracted from RecordTable so RecordBoard (a second renderer of the same
 * generic list, grouped into lanes instead of rows) can share it rather
 * than issuing a parallel fetch of its own. `no-dupes`/`ui-reviewer` both
 * call this out: a board is the same list, grouped differently, not a
 * second data path.
 */
export interface RecordListState {
  result: ListResult | null
  loading: boolean
  error: string | null
  setResult: (updater: (prev: ListResult | null) => ListResult | null) => void
}

export function useRecordList(
  entity: string,
  filters: FilterNode | null,
  sort: SortSpec[] | null,
  refreshToken: number,
  limit = 200,
): RecordListState {
  const [result, setResultState] = useState<ListResult | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    const path = withQuery(`/records/${entity}`, {
      filter: filters ?? undefined,
      sort,
      limit,
    })
    apiGet<ListResult>(path)
      .then((r) => {
        if (!cancelled) setResultState(r)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [entity, filters, sort, refreshToken, limit])

  function setResult(updater: (prev: ListResult | null) => ListResult | null) {
    setResultState(updater)
  }

  return { result, loading, error, setResult }
}
