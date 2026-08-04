import { Link } from 'react-router-dom'
import { HierarchyChain } from './HierarchyChain'
import { LinkRecordControl } from './LinkRecordControl'
import { RecordFieldList } from './RecordFieldList'
import { RelatedPanel } from './RelatedPanel'
import { useRecordDetail } from './useRecordDetail'
import type { EntitySchema } from './types'

interface RecordDetailProps {
  entity: string
  recordId: string
  schema: EntitySchema
  onDeleted: () => void
  onClose: () => void
}

/** The split-view record panel `EntityListPage.tsx` opens beside its table
 * (`/e/:entity/:recordId`) -- header, fields, hierarchy chains and the
 * related panel, for a quick look or edit without leaving the list. The
 * dedicated `/r/:entity/:id` page (`RecordPage.tsx`, Phase 5) is where the
 * same data gets its own full-page, three-region layout; both share the
 * loading/mutation logic through `useRecordDetail()` rather than each
 * re-implementing it (R1). */
export function RecordDetail({ entity, recordId, schema, onDeleted, onClose }: RecordDetailProps) {
  const {
    record, related, error, ready, hierarchicalRoles, loadRelated,
    saveField, savingField, editingField, setEditingField, labelFor, confirmAndDelete,
  } = useRecordDetail(entity, recordId, schema)

  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (!ready || !record) return <div className="crm-table-status">Loading…</div>

  const title = String(record[schema.label_field] ?? record.id)

  return (
    <div className="crm-record-detail">
      <div className="crm-record-detail-header">
        <button className="crm-detail-close" onClick={onClose} aria-label="Close">
          ×
        </button>
        <Link className="crm-detail-expand" to={`/r/${entity}/${recordId}`} title="Open full page">
          ⤢
        </Link>
        <h2>{title}</h2>
        <button className="crm-detail-delete" onClick={() => confirmAndDelete(onDeleted)}>
          Delete
        </button>
      </div>

      {hierarchicalRoles.length > 0 && (
        <div className="crm-record-detail-hierarchy">
          {hierarchicalRoles.map((r) => (
            <HierarchyChain key={r.role} entity={entity} recordId={recordId} role={r.role} label={r.label} />
          ))}
        </div>
      )}

      <div className="crm-record-detail-body">
        <RecordFieldList
          schema={schema}
          record={record}
          editingField={editingField}
          setEditingField={setEditingField}
          saveField={saveField}
          savingField={savingField}
          labelFor={labelFor}
        />

        <div className="crm-record-detail-related">
          <div className="crm-record-detail-related-header">
            <h3>Related</h3>
            <LinkRecordControl entity={entity} recordId={recordId} onLinked={loadRelated} />
          </div>
          <RelatedPanel related={related} onChanged={loadRelated} />
        </div>
      </div>
    </div>
  )
}
