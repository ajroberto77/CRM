import { useEffect, useState } from 'react'
import { apiPost } from '../lib/api'

/** Module-level, shared across every component using this hook (not a
 * per-component cache) -- a fund named on ten rows across the app resolves
 * once per session, not once per row per mount. Labels change rarely enough
 * (an admin renaming a picklist entry) that never invalidating within a
 * session is an acceptable tradeoff against re-fetching on every render.
 * `attempted` remembers a ref that resolved to NO label (not visible, or
 * genuinely gone) so it is asked for exactly once, not on every render that
 * still includes it -- POST /records/labels silently omits it every time,
 * and without this a component that keeps referencing it would refetch on
 * every render that changed `missingKey` even though the answer never
 * changes for the current session. */
const labelCache = new Map<string, string>()
const attempted = new Set<string>()
const cacheKey = (entity: string, id: string) => `${entity}:${id}`

export interface RecordRef {
  entity: string
  id: string | null | undefined
}

/** Resolves a page's worth of reference-field values to human labels via
 * one `POST /records/labels` call per distinct (missing) set (decision B) --
 * instead of N per-row schema/list calls, and it works for an `admin_only`
 * reference table a non-admin cannot otherwise read at all.
 *
 * Returns a lookup function rather than a map, so a component reads labels
 * during render without needing the caller to thread a map through props;
 * a cache hit (the common case once a page has settled) costs nothing extra.
 */
export function useRecordLabels(refs: RecordRef[]): {
  labelFor: (entity: string, id: string | null | undefined) => string | null
  loading: boolean
} {
  const [, forceRender] = useState(0)
  const [loading, setLoading] = useState(false)

  const missing = new Map<string, Set<string>>()
  for (const { entity, id } of refs) {
    if (!id) continue
    const key = cacheKey(entity, id)
    if (labelCache.has(key) || attempted.has(key)) continue
    if (!missing.has(entity)) missing.set(entity, new Set())
    missing.get(entity)!.add(id)
  }
  const missingKey = [...missing.entries()]
    .map(([entity, ids]) => `${entity}:${[...ids].sort().join(',')}`)
    .join('|')

  useEffect(() => {
    if (missing.size === 0) return
    setLoading(true)
    const body: Record<string, string[]> = {}
    const attemptedThisCall: string[] = []
    for (const [entity, ids] of missing.entries()) {
      body[entity] = [...ids]
      for (const id of ids) {
        const key = cacheKey(entity, id)
        attempted.add(key)
        attemptedThisCall.push(key)
      }
    }
    apiPost<{ labels: Record<string, Record<string, string>> }>('/records/labels', { refs: body })
      .then((res) => {
        for (const [entity, byId] of Object.entries(res.labels)) {
          for (const [id, label] of Object.entries(byId)) {
            labelCache.set(cacheKey(entity, id), label)
          }
        }
        forceRender((n) => n + 1)
      })
      .catch(() => {
        // A network/server failure, not "not visible" -- safety rule 5's
        // "no per-process latch on recoverable state" applies here just as
        // much as to an external host check: un-mark these refs so the next
        // render with the same missing set retries, instead of every one of
        // them silently rendering a raw uuid for the rest of the tab's life.
        for (const key of attemptedThisCall) attempted.delete(key)
        forceRender((n) => n + 1)
      })
      .finally(() => setLoading(false))
    // `missing` is derived fresh every render from `refs` + the module-level
    // caches; the effect only needs to refire when the SET of still-unknown
    // refs actually changes, which `missingKey` captures by content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [missingKey])

  function labelFor(entity: string, id: string | null | undefined): string | null {
    if (!id) return null
    return labelCache.get(cacheKey(entity, id)) ?? null
  }

  return { labelFor, loading }
}
