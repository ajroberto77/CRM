import { Link } from 'react-router-dom'
import type { AggregateGroup } from './useAggregate'

interface DashboardTileProps {
  title: string
  groups: AggregateGroup[]
  linkTo: string
  formatValue?: (value: number) => string
}

/** One "grouped stat row" dashboard tile -- a title plus one linked stat per
 * group, reduced at the database layer via `useAggregate()`. Shared by
 * `HomePage.tsx`'s "Deals by status" tile and `VerticalDashboard.tsx`'s
 * tiles (Phase 12) so there is exactly one tile-rendering implementation
 * (R1) instead of each dashboard reinventing the same markup. Renders
 * nothing for an empty group list -- the caller's own loading/unauthorized
 * gating happens before this, so "no groups" here means "genuinely empty,"
 * not "still loading." */
export function DashboardTile({ title, groups, linkTo, formatValue }: DashboardTileProps) {
  if (groups.length === 0) return null
  return (
    <div className="crm-home-tile crm-home-tile-wide">
      <div className="crm-home-tile-label">{title}</div>
      <div className="crm-home-stat-row">
        {groups.map((g) => (
          <Link to={linkTo} key={String(g.key)} className="crm-home-stat">
            <div className="crm-home-stat-value">{formatValue ? formatValue(g.value) : g.value}</div>
            <div className="crm-home-stat-label">{String(g.key)}</div>
          </Link>
        ))}
      </div>
    </div>
  )
}
