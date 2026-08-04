import { Link } from 'react-router-dom'
import { formatDateTime, recordLabel } from '../lib/format'
import { useEntitySchema } from './useEntitySchema'
import { useTimeline } from './useTimeline'
import type { TimelineEntry } from './types'

interface TimelineProps {
  entity: string
  recordId: string
}

const ENTITY_ICONS: Record<string, string> = {
  interaction: '✉',
  note: '📝',
  task: '☑',
  document: '📄',
  contact_channel: '☎',
}

/** The merged activity feed (Phase 8): notes/tasks/documents plus, for a
 * person, their interactions, in one chronological list -- what
 * `ChildrenPanel`'s per-type blocks look like collapsed into a single
 * timeline instead of grouped by kind. */
export function Timeline({ entity, recordId }: TimelineProps) {
  const { entries, loading, error } = useTimeline(entity, recordId)

  if (loading) return <div className="crm-table-status">Loading…</div>
  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (entries.length === 0) return <div className="crm-detail-empty">No activity yet.</div>

  return (
    <div className="crm-timeline">
      {entries.map((entry) => (
        <TimelineRow key={`${entry.entity}:${entry.record.id}`} entry={entry} />
      ))}
    </div>
  )
}

/** One row's own entity type resolves its `label_field` through the same
 * cached `useEntitySchema` every other component uses (R1) -- a flat
 * merged list can repeat the same entity type across many rows, and the
 * cache `useEntitySchema` itself owns means only the first row of each
 * type actually fetches, not one request per row. `recordLabel()` needs
 * the real `label_field` to render e.g. an interaction's computed
 * `display_label` ("Email — Aug 4, 2026") instead of falling through to a
 * raw id. */
function TimelineRow({ entry }: { entry: TimelineEntry }) {
  const { schema } = useEntitySchema(entry.entity)

  return (
    <div className="crm-timeline-entry">
      <span className="crm-timeline-icon" aria-hidden="true">
        {ENTITY_ICONS[entry.entity] ?? '•'}
      </span>
      <div className="crm-timeline-body">
        <Link to={`/r/${entry.entity}/${entry.record.id}`} className="crm-reference-link">
          {recordLabel(entry.record, schema?.label_field)}
        </Link>
        {entry.occurred_at && <span className="crm-timeline-time">{formatDateTime(entry.occurred_at)}</span>}
      </div>
    </div>
  )
}
