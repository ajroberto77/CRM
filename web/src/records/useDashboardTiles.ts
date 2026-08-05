import { useEffect, useState } from 'react'
import { apiGet, withQuery } from '../lib/api'

export interface DashboardTileSchema {
  key: string
  title: string
  entity: string
  group_by: string
  metric: 'count' | 'sum' | 'avg' | 'min' | 'max'
  metric_field: string | null
  order: number
}

/** Fetches GET /records/dashboard-tiles?nav_group=... -- every tile
 * registered for one vertical (`registry.register_dashboard_tile()`) the
 * signed-in principal can at least read the entity of. `VerticalDashboard.tsx`
 * builds from this list, never a hardcoded tile set (R6). */
export function useDashboardTiles(navGroup: string) {
  const [tiles, setTiles] = useState<DashboardTileSchema[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiGet<{ tiles: DashboardTileSchema[] }>(withQuery('/records/dashboard-tiles', { nav_group: navGroup }))
      .then((res) => {
        if (!cancelled) setTiles(res.tiles)
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
  }, [navGroup])

  return { tiles, loading, error }
}

/** Fetches GET /records/dashboard-groups -- every `nav_group` with at least
 * one registered dashboard tile, so a vertical dashboard route/nav entry
 * exists once any module registers a tile for it, with no fixed list of
 * verticals hardcoded in the frontend (R6). */
export function useDashboardNavGroups() {
  const [navGroups, setNavGroups] = useState<string[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    apiGet<{ nav_groups: string[] }>('/records/dashboard-groups')
      .then((res) => {
        if (!cancelled) setNavGroups(res.nav_groups)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return { navGroups, loading }
}
