import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { formatCurrency } from '../lib/format'
import { DashboardTile } from '../records/DashboardTile'
import { useAggregate } from '../records/useAggregate'
import type { AggregateGroup } from '../records/useAggregate'
import { useDashboardTiles } from '../records/useDashboardTiles'
import type { DashboardTileSchema } from '../records/useDashboardTiles'

interface TileResult {
  groups: AggregateGroup[]
  unauthorized: boolean
}

/** One registered tile's own `useAggregate` call -- a separate component so
 * each tile in a dynamic, variable-length list gets its own stable,
 * unconditional hook call (the same reasoning `EntityNavItem` in Shell.tsx
 * documents for its own per-entity hook call inside a `.map()`). Renders
 * nothing itself -- it only reports its settled result up to the parent,
 * which paints every tile atomically (see `VerticalDashboard`'s own
 * comment on why). */
function VerticalDashboardTile({
  tile, onSettled,
}: {
  tile: DashboardTileSchema
  onSettled: (key: string, result: TileResult) => void
}) {
  const agg = useAggregate(tile.entity, {
    group_by: tile.group_by, metric: tile.metric, metric_field: tile.metric_field ?? undefined,
  })
  useEffect(() => {
    if (!agg.loading) onSettled(tile.key, { groups: agg.groups, unauthorized: agg.unauthorized })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agg.loading])
  return null
}

/** `/dashboard/:navGroup` -- a per-vertical dashboard built entirely from
 * `registry.register_dashboard_tile()` entries (`useDashboardTiles`), never
 * a hardcoded per-vertical page. A module that registers a tile for a new
 * `nav_group` gets a working dashboard at this route with no frontend
 * change (R6), the same registry-driven contract `EntityListPage`/
 * `RecordPage` already give a new entity.
 *
 * Tiles paint together, not as each one's own `useAggregate` happens to
 * settle -- `HomePage.tsx`'s own `pageLoading` gate exists because
 * independently-arriving tiles visibly reflow `.crm-home-tiles`' auto-fill
 * grid as each one pops in; this dashboard reuses that same grid and would
 * have the identical defect without the same one-gate treatment. */
export function VerticalDashboard() {
  const { navGroup = '' } = useParams<{ navGroup: string }>()
  const { tiles, loading, error } = useDashboardTiles(navGroup)
  const [results, setResults] = useState<Record<string, TileResult>>({})

  const handleSettled = useCallback((key: string, result: TileResult) => {
    setResults((prev) => (prev[key] ? prev : { ...prev, [key]: result }))
  }, [])

  const allSettled = tiles.length > 0 && tiles.every((t) => results[t.key])

  return (
    <div className="crm-home-page">
      <div className="crm-entity-page-header">
        <h1>{navGroup}</h1>
      </div>
      {/* Mounted unconditionally (even while "Loading…" shows below) so
          every tile's own `useAggregate` fetch starts right away instead of
          waiting for `allSettled` -- only the VISIBLE grid waits for all of
          them, not the fetches themselves. */}
      {tiles.map((tile) => (
        <VerticalDashboardTile key={tile.key} tile={tile} onSettled={handleSettled} />
      ))}

      {loading || (tiles.length > 0 && !allSettled) ? (
        <div className="crm-table-status">Loading…</div>
      ) : error ? (
        <div className="crm-table-status crm-table-status-error">{error}</div>
      ) : tiles.length === 0 ? (
        <div className="crm-detail-empty">No dashboard tiles registered for {navGroup}.</div>
      ) : (
        <div className="crm-home-tiles">
          {tiles.map((tile) => {
            const result = results[tile.key]
            if (result.unauthorized) return null
            // `count` groups are plain integers; every other metric this
            // platform's tiles reduce is a currency amount (amount/
            // target_size/...) -- there is no currency-vs-plain-number
            // distinction carried on DashboardTileSchema itself, so this is
            // a simplification, not a general-purpose formatter.
            const formatValue = tile.metric === 'count' ? undefined : formatCurrency
            return (
              <DashboardTile
                key={tile.key} title={tile.title} groups={result.groups}
                linkTo={`/e/${tile.entity}`} formatValue={formatValue}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
