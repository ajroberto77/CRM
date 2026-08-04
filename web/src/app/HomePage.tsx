import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, withQuery } from '../lib/api'
import { useAuth } from '../auth/AuthContext'
import { formatCurrency } from '../lib/format'
import { useAggregate } from '../records/useAggregate'
import type { ListResult, RecordRow } from '../records/types'

/** The landing page -- first, top-left item in the sidebar. The proposals
 * count and the signed-in user's own open tasks are cheap to compute from
 * the existing generic /records list endpoint (no new backend route). The
 * deal tiles are the first real dashboard tiles (Phase 6): grouped/summed
 * at the database layer through `useAggregate()`, not a full list fetched
 * and reduced client-side the way a "cheap approximation" would.
 *
 * `deal` is core's own entity (R6: no fund/investor-specific tile lives
 * here -- a module's own dashboard contribution is a follow-up, not
 * something this file should hardcode). Both deal tiles degrade to hidden,
 * not an error, if the signed-in user can't read `deal` at all. */
export function HomePage() {
  const { user } = useAuth()
  const [pendingCount, setPendingCount] = useState<number | null>(null)
  const [myTasks, setMyTasks] = useState<RecordRow[]>([])
  const [loading, setLoading] = useState(true)

  const dealCounts = useAggregate('deal', { group_by: 'status', metric: 'count' })
  const dealAmounts = useAggregate('deal', { group_by: 'status', metric: 'sum', metric_field: 'amount' })
  const openPipelineValue = dealAmounts.groups.find((g) => g.key === 'open')?.value ?? 0

  useEffect(() => {
    if (!user) return
    let cancelled = false
    setLoading(true)
    Promise.all([
      apiGet<ListResult>(
        withQuery('/records/proposed_change', {
          filter: { field: 'status', op: 'eq', value: 'pending' },
          limit: 1,
        }),
      ),
      apiGet<ListResult>(
        withQuery('/records/task', {
          filter: {
            and: [
              { field: 'owner_id', op: 'eq', value: user.id },
              { field: 'status', op: 'eq', value: 'open' },
            ],
          },
          sort: [{ field: 'due_on', direction: 'asc' }],
          limit: 25,
        }),
      ),
    ])
      .then(([proposals, tasks]) => {
        if (cancelled) return
        setPendingCount(proposals.total)
        setMyTasks(tasks.records)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [user])

  const today = new Date().toISOString().slice(0, 10)

  // One gate for every tile's data, not just the proposals/tasks fetch --
  // the deal tiles used to pop in independently a beat after everything
  // else had already painted, which (confirmed with .crm-home-tiles' own
  // auto-fill grid at a narrow viewport width) visibly pushed "My open
  // tasks" down a row once they arrived. All tiles now appear atomically.
  const pageLoading = loading || dealCounts.loading || dealAmounts.loading

  return (
    <div className="crm-home-page">
      <div className="crm-entity-page-header">
        <h1>Home</h1>
      </div>
      {pageLoading ? (
        <div className="crm-table-status">Loading…</div>
      ) : (
        <div className="crm-home-tiles">
          <Link to="/review/proposals" className="crm-home-tile">
            <div className="crm-home-tile-value">{pendingCount ?? 0}</div>
            <div className="crm-home-tile-label">Scheduling suggestions</div>
          </Link>

          {!dealAmounts.unauthorized && (
            <Link to="/e/deal" className="crm-home-tile">
              <div className="crm-home-tile-value">{formatCurrency(openPipelineValue)}</div>
              <div className="crm-home-tile-label">Open pipeline value</div>
            </Link>
          )}

          <div className="crm-home-tile crm-home-tile-wide">
            <div className="crm-home-tile-label">My open tasks</div>
            {myTasks.length === 0 ? (
              <div className="crm-detail-empty">Nothing open.</div>
            ) : (
              <ul className="crm-home-task-list">
                {myTasks.map((t) => (
                  <li key={String(t.id)}>
                    <Link to={`/e/task/${t.id}`}>{String(t.title ?? '(untitled)')}</Link>
                    {typeof t.due_on === 'string' && (
                      <span className={t.due_on < today ? 'crm-home-task-overdue' : 'crm-home-task-due'}>
                        {t.due_on < today ? 'overdue ' : 'due '}
                        {t.due_on}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {!dealCounts.unauthorized && dealCounts.groups.length > 0 && (
            <div className="crm-home-tile crm-home-tile-wide">
              <div className="crm-home-tile-label">Deals by status</div>
              <div className="crm-home-stat-row">
                {dealCounts.groups.map((g) => (
                  <Link to="/e/deal" key={String(g.key)} className="crm-home-stat">
                    <div className="crm-home-stat-value">{g.value}</div>
                    <div className="crm-home-stat-label">{String(g.key)}</div>
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
