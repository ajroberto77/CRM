import { useState } from 'react'
import { Link } from 'react-router-dom'
import { apiDelete, apiPost, ApiError } from '../lib/api'
import { formatDateRange, recordLabel } from '../lib/format'
import type { RelatedBlocks } from './types'

interface RelatedPanelProps {
  related: RelatedBlocks
  onChanged: () => Promise<void>
}

/** The related-records panel body: every association edge, grouped by the
 * role's own `group`/`group_order` (server/core/registry.py's
 * `AssociationRole.group`, resolved through `role_presentation()` and
 * carried on each `RelatedItem` -- Phase 4), sub-labeled per role/direction
 * within a group, each item showing its date range (if any) and End/Delete
 * actions. One generic renderer (R1/R4): a new role registered by any
 * module groups and sorts correctly with no change here. */
export function RelatedPanel({ related, onChanged }: RelatedPanelProps) {
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)

  const labels = Object.keys(related)
  if (labels.length === 0) return <div className="crm-detail-empty">No associations.</div>

  // Every item under one label shares a role+direction (edges_for groups by
  // exactly that), so the first item's group/group_order speaks for the
  // whole label.
  const groupOf = (label: string) => {
    const first = related[label][0]
    return { group: first?.group || '', order: first?.group_order ?? 100 }
  }
  const sortedLabels = [...labels].sort((a, b) => {
    const ga = groupOf(a)
    const gb = groupOf(b)
    if (ga.order !== gb.order) return ga.order - gb.order
    if (ga.group !== gb.group) return ga.group.localeCompare(gb.group)
    return a.localeCompare(b)
  })
  const groupsInOrder: string[] = []
  for (const label of sortedLabels) {
    const { group } = groupOf(label)
    if (!groupsInOrder.includes(group)) groupsInOrder.push(group)
  }

  async function handleEnd(associationId: string) {
    if (!window.confirm('End this relationship as of today? It stays in history.')) return
    setBusyId(associationId)
    setError(null)
    try {
      await apiPost(`/associations/${associationId}/end`, {})
      // Awaited: the button must stay disabled until `related` itself
      // reflects the change, or its busy state clears while the panel
      // still shows the stale (not-yet-ended) row, inviting a double-click.
      await onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to end relationship')
    } finally {
      setBusyId(null)
    }
  }

  async function handleDelete(associationId: string) {
    if (!window.confirm('Delete this relationship outright? This cannot be undone.')) return
    setBusyId(associationId)
    setError(null)
    try {
      await apiDelete(`/associations/${associationId}`)
      await onChanged()
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to delete relationship')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <>
      {error && <div className="crm-auth-error">{error}</div>}
      {groupsInOrder.map((group) => (
        <div className="crm-related-group" key={group || '__default'}>
          {group && <div className="crm-related-group-header">{group}</div>}
          {sortedLabels
            .filter((label) => groupOf(label).group === group)
            .map((label) => (
              <div className="crm-related-block" key={label}>
                <div className="crm-related-block-label">{label}</div>
                {related[label].map((item) => (
                  <div className="crm-related-item" key={item.association_id}>
                    <Link to={`/e/${item.entity}/${item.record.id}`} className="crm-reference-link">
                      {recordLabel(item.record)}
                    </Link>
                    {(item.valid_from || item.valid_to) && (
                      <span className="crm-related-item-dated">
                        {formatDateRange(item.valid_from, item.valid_to)}
                      </span>
                    )}
                    <span className="crm-related-item-actions">
                      {!item.valid_to && (
                        <button
                          className="crm-related-item-action"
                          disabled={busyId === item.association_id}
                          onClick={() => handleEnd(item.association_id)}
                        >
                          End
                        </button>
                      )}
                      <button
                        className="crm-related-item-action crm-related-item-action-delete"
                        disabled={busyId === item.association_id}
                        onClick={() => handleDelete(item.association_id)}
                      >
                        Delete
                      </button>
                    </span>
                  </div>
                ))}
              </div>
            ))}
        </div>
      ))}
    </>
  )
}
