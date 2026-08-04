import { useEffect, useState } from 'react'
import { apiPost, ApiError } from '../lib/api'
import type { FilterNode } from './types'

export interface AggregateGroup {
  key: string | number | boolean | null
  value: number
}

interface AggregateParams {
  group_by: string
  metric?: 'count' | 'sum' | 'avg' | 'min' | 'max'
  metric_field?: string
  filters?: FilterNode | null
  limit?: number
}

/** Fetches POST /records/{entity}/aggregate -- a dashboard tile's one data
 * source, reduced at the database layer (not a full list fetched and
 * summed client-side, the "cheap approximation" HomePage.tsx's own history
 * deliberately avoided until this existed) so a tile stays correct under
 * visibility scoping without pulling every row over the wire.
 *
 * `unauthorized` is split out from `error` (rather than leaving the caller
 * to string-match) so a tile whose entity the signed-in user simply can't
 * read (e.g. a restricted role) can degrade to "not shown" instead of an
 * error banner -- the same 403-swallow precedent `LinkRecordControl.tsx`'s
 * `gp_role` fetch already established for exactly this kind of expected,
 * permission-shaped absence. */
export function useAggregate(entity: string, params: AggregateParams) {
  const [groups, setGroups] = useState<AggregateGroup[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [unauthorized, setUnauthorized] = useState(false)

  const paramsKey = JSON.stringify(params)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setUnauthorized(false)
    apiPost<{ groups: AggregateGroup[] }>(`/records/${entity}/aggregate`, JSON.parse(paramsKey))
      .then((r) => {
        if (!cancelled) setGroups(r.groups)
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 403) {
          setUnauthorized(true)
          return
        }
        setError(err instanceof ApiError ? err.detail : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
    // paramsKey (a JSON-stable stand-in for `params`) is the real dependency --
    // a fresh object literal every render would otherwise refetch every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entity, paramsKey])

  return { groups, loading, error, unauthorized }
}
