import { useEffect, useState } from 'react'
import { apiGet } from '../lib/api'
import type { AssociationRoleOption, EntitySchema, EntitySummary } from './types'

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

// Module-level cache for entity list, keyed by org context at runtime
// (no cache key needed: one org per session). Retries on network failure
// instead of permanently latching a stale/unresolved state (safety rule 5).
let entityListCache: { entities: EntitySummary[] } | null = null
let entityListPromise: Promise<{ entities: EntitySummary[] }> | null = null

export function useEntityList(): { entities: EntitySummary[]; loading: boolean; error: string | null } {
  const [entities, setEntities] = useState<EntitySummary[]>([])
  const [loading, setLoading] = useState(!entityListCache)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (entityListCache) {
      setEntities(entityListCache.entities)
      return
    }

    let cancelled = false

    const load = async () => {
      try {
        if (!entityListPromise) {
          entityListPromise = apiGet<{ entities: EntitySummary[] }>('/records')
        }
        const result = await entityListPromise
        if (!cancelled) {
          entityListCache = result
          setEntities(result.entities)
          setError(null)
        }
      } catch (err) {
        if (!cancelled) {
          entityListPromise = null
          setError(err instanceof Error ? err.message : String(err))
        }
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    load()
    return () => {
      cancelled = true
    }
  }, [])

  return { entities, loading, error }
}

/** Fetches GET /records/{entity}/roles -- every association role `entity`
 * can take part in, from either side. `RecordDetail.tsx` uses this to find
 * which hierarchical roles apply (driving `HierarchyChain` generically,
 * with no per-role/per-entity special case -- R6) without duplicating
 * `LinkRecordControl.tsx`'s own fetch of the same endpoint (that one stays
 * lazy, fetching only once its "+ Link" form opens). */
export function useEntityRoles(entity: string | undefined): {
  roles: AssociationRoleOption[]
  loading: boolean
  error: string | null
} {
  const [roles, setRoles] = useState<AssociationRoleOption[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!entity) return
    let cancelled = false
    setLoading(true)
    setError(null)
    apiGet<{ roles: AssociationRoleOption[] }>(`/records/${entity}/roles`)
      .then((r) => {
        if (!cancelled) setRoles(r.roles)
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

  return { roles, loading, error }
}
