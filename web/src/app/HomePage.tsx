import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, withQuery } from '../lib/api'
import { useAuth } from '../auth/AuthContext'
import type { ListResult, RecordRow } from '../records/types'

/** The landing page -- first, top-left item in the sidebar. Deliberately
 * starts small: pending proposals and the signed-in user's own open tasks,
 * both cheap to compute from the existing generic /records list endpoint
 * (no new backend route). Richer tiles (fund raise progress, re-up risk,
 * accreditation expiry) need a real aggregate endpoint and are a follow-up,
 * not invented here as client-side approximations. */
export function HomePage() {
  const { user } = useAuth()
  const [pendingCount, setPendingCount] = useState<number | null>(null)
  const [myTasks, setMyTasks] = useState<RecordRow[]>([])
  const [loading, setLoading] = useState(true)

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

  return (
    <div className="crm-home-page">
      <div className="crm-entity-page-header">
        <h1>Home</h1>
      </div>
      {loading ? (
        <div className="crm-table-status">Loading…</div>
      ) : (
        <div className="crm-home-tiles">
          <Link to="/review/proposals" className="crm-home-tile">
            <div className="crm-home-tile-value">{pendingCount ?? 0}</div>
            <div className="crm-home-tile-label">Pending proposals</div>
          </Link>

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
        </div>
      )}
    </div>
  )
}
