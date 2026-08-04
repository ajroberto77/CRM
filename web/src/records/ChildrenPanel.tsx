import { Link } from 'react-router-dom'
import { recordLabel } from '../lib/format'
import { useEntitySchema } from './useEntitySchema'
import { useRecordChildren } from './useRecordChildren'
import type { ChildrenBlock } from './types'

interface ChildrenPanelProps {
  entity: string
  recordId: string
}

/** The record page's center region (Phase 5): every record naming this one
 * as its parent through a real FK field -- a fund's commitments, a
 * person's tasks -- resolved entirely from `registry.reverse_references()`
 * on the backend, so a new child field needs no change here (R4/R6: no
 * entity name is ever hardcoded). Reuses `.crm-related-block`'s grouped-
 * list styling rather than inventing a second "labeled group of linked
 * rows" shape for what is visually the same thing as the related panel. */
export function ChildrenPanel({ entity, recordId }: ChildrenPanelProps) {
  const { blocks, loading, error } = useRecordChildren(entity, recordId)

  if (loading) return <div className="crm-table-status">Loading…</div>
  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (blocks.length === 0) return <div className="crm-detail-empty">No child records.</div>

  return (
    <div className="crm-children-panel">
      {blocks.map((block) => (
        <ChildBlock key={`${block.entity}:${block.field}`} block={block} />
      ))}
    </div>
  )
}

function ChildBlock({ block }: { block: ChildrenBlock }) {
  const { schema } = useEntitySchema(block.entity)
  if (!schema) return null

  return (
    <div className="crm-related-block">
      <div className="crm-related-block-label">
        {schema.label} ({block.total})
      </div>
      {block.records.map((r) => (
        <div className="crm-related-item" key={String(r.id)}>
          <Link to={`/r/${block.entity}/${r.id}`} className="crm-reference-link">
            {recordLabel(r, schema.label_field)}
          </Link>
        </div>
      ))}
      {block.total > block.records.length && (
        <div className="crm-children-block-more">
          <Link to={`/e/${block.entity}`} className="crm-reference-link">
            View all {block.total}
          </Link>
        </div>
      )}
    </div>
  )
}
