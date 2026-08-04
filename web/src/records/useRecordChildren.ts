import { useEffect, useState } from 'react'
import { apiGet, ApiError } from '../lib/api'
import type { ChildrenBlock } from './types'

/** Fetches GET /records/{entity}/{id}/children -- records naming this one
 * as their parent through a real FK field, grouped one block per child
 * entity type. The FK-based counterpart to `useRecordDetail`'s `related`
 * (association edges). */
export function useRecordChildren(entity: string, recordId: string) {
  const [blocks, setBlocks] = useState<ChildrenBlock[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiGet<{ children: ChildrenBlock[] }>(`/records/${entity}/${recordId}/children`)
      .then((r) => {
        if (!cancelled) setBlocks(r.children)
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

  return { blocks, loading, error }
}
