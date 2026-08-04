import { useEffect, useState } from 'react'
import { apiGet, ApiError } from '../lib/api'
import type { TimelineEntry } from './types'

/** Fetches GET /records/{entity}/{id}/timeline -- a record's merged,
 * chronological activity feed (children plus, for a person, their
 * interactions), already sorted newest-first server-side. */
export function useTimeline(entity: string, recordId: string) {
  const [entries, setEntries] = useState<TimelineEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiGet<{ entries: TimelineEntry[] }>(`/records/${entity}/${recordId}/timeline`)
      .then((r) => {
        if (!cancelled) setEntries(r.entries)
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
  }, [entity, recordId])

  return { entries, loading, error }
}
