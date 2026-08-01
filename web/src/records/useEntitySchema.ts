import { useEffect, useState } from 'react'
import { apiGet } from '../lib/api'
import type { EntitySchema, EntitySummary } from './types'

interface SchemaState {
  schema: EntitySchema | null
  loading: boolean
  error: string | null
}

/** Fetches GET /records/{entity}/schema -- the single source every
 * table/form/filter builds from, so a new entity or custom field needs no
 * frontend change to appear (R4). */
export function useEntitySchema(entity: string | undefined): SchemaState {
  const [schema, setSchema] = useState<EntitySchema | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!entity) return
    let cancelled = false
    setLoading(true)
    setError(null)
    apiGet<EntitySchema>(`/records/${entity}/schema`)
      .then((s) => {
        if (!cancelled) setSchema(s)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err))
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [entity])

  return { schema, loading, error }
}

export function useEntityList(): { entities: EntitySummary[]; loading: boolean } {
  const [entities, setEntities] = useState<EntitySummary[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    apiGet<{ entities: EntitySummary[] }>('/records')
      .then((r) => setEntities(r.entities))
      .finally(() => setLoading(false))
  }, [])

  return { entities, loading }
}
