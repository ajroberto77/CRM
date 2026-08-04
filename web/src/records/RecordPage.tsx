import { Link, useNavigate, useParams } from 'react-router-dom'
import { ChildrenPanel } from './ChildrenPanel'
import { HierarchyChain } from './HierarchyChain'
import { LinkRecordControl } from './LinkRecordControl'
import { RecordFieldList } from './RecordFieldList'
import { RelatedPanel } from './RelatedPanel'
import { useEntitySchema } from './useEntitySchema'
import { useRecordDetail } from './useRecordDetail'
import type { EntitySchema } from './types'

/** Keyed by `entity:id` in the export below so navigating from one record's
 * page to another (a reference link, a related item, a hierarchy step)
 * fully remounts this component -- same reasoning as `EntityListPage.tsx`'s
 * own per-entity remount: without it, state set inside a `useEffect` would
 * render one frame with the NEW id but the PREVIOUS record's data. */
function RecordPageForRecord() {
  const { entity: entityParam = '', recordId = '' } = useParams<{ entity: string; recordId: string }>()
  const entity = entityParam.replace(/-/g, '_')
  const navigate = useNavigate()
  const { schema, loading: schemaLoading, error: schemaError } = useEntitySchema(entity)

  if (schemaLoading) return <div className="crm-table-status">Loading…</div>
  if (schemaError || !schema) {
    return <div className="crm-table-status crm-table-status-error">{schemaError ?? 'Unknown entity'}</div>
  }

  return <RecordPageBody entity={entity} recordId={recordId} schema={schema} onDeleted={() => navigate(`/e/${entity}`)} />
}

function RecordPageBody({
  entity, recordId, schema, onDeleted,
}: {
  entity: string
  recordId: string
  schema: EntitySchema
  onDeleted: () => void
}) {
  const {
    record, related, error, ready, hierarchicalRoles, loadRelated,
    saveField, savingField, editingField, setEditingField, labelFor, confirmAndDelete,
  } = useRecordDetail(entity, recordId, schema)

  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (!ready || !record) return <div className="crm-table-status">Loading…</div>

  const title = String(record[schema.label_field] ?? record.id)

  return (
    <div className="crm-record-page">
      <div className="crm-record-page-header">
        <Link to={`/e/${entity}`} className="crm-record-page-back">
          ← {schema.label}
        </Link>
        <h1>{title}</h1>
        <button className="crm-detail-delete" onClick={() => confirmAndDelete(onDeleted)}>
          Delete
        </button>
      </div>

      <div className="crm-record-page-body">
        <div className="crm-record-page-left">
          {hierarchicalRoles.length > 0 && (
            <div className="crm-record-detail-hierarchy">
              {hierarchicalRoles.map((r) => (
                <HierarchyChain key={r.role} entity={entity} recordId={recordId} role={r.role} label={r.label} />
              ))}
            </div>
          )}
          <RecordFieldList
            schema={schema}
            record={record}
            editingField={editingField}
            setEditingField={setEditingField}
            saveField={saveField}
            savingField={savingField}
            labelFor={labelFor}
          />
        </div>

        <div className="crm-record-page-center">
          <h3>Records</h3>
          <ChildrenPanel entity={entity} recordId={recordId} />
        </div>

        <div className="crm-record-page-right">
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

export function RecordPage() {
  const { entity, recordId } = useParams<{ entity: string; recordId: string }>()
  return <RecordPageForRecord key={`${entity}:${recordId}`} />
}
