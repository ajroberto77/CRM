import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ChildrenPanel } from './ChildrenPanel'
import { HierarchyChain } from './HierarchyChain'
import { LinkRecordControl } from './LinkRecordControl'
import { getProfileBlock } from './profileBlocks'
import { RecordFieldList } from './RecordFieldList'
import { RelatedPanel } from './RelatedPanel'
import { Timeline } from './Timeline'
import { useEntitySchema } from './useEntitySchema'
import { useRecordDetail } from './useRecordDetail'
import type { EntitySchema, ProfileBlockSchema, RoleSummaryEntry } from './types'

/** `schema.profile_blocks` filtered to whatever actually applies to THIS
 * record: a `roles` gate checks the record's live `role_summary` (empty
 * `roles` means no such gate), and `show_if_field`/`show_if_equals` checks
 * a plain field value -- both must pass when both are set. Pure filtering
 * logic pulled out of the component so it's easy to reason about/test
 * independent of rendering. */
function eligibleProfileBlocks(
  blocks: ProfileBlockSchema[],
  currentRoleNames: Set<string>,
  record: Record<string, unknown>,
): ProfileBlockSchema[] {
  return blocks.filter((block) => {
    if (block.roles.length > 0 && !block.roles.some((r) => currentRoleNames.has(r))) return false
    if (block.show_if_field && record[block.show_if_field] !== block.show_if_equals) return false
    return true
  })
}

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
  const [centerTab, setCenterTab] = useState<'timeline' | 'records'>('timeline')

  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (!ready || !record) return <div className="crm-table-status">Loading…</div>

  // A plain `const` re-binding so its static type is the narrowed `RecordRow`
  // (not `RecordRow | null`) independent of closures below -- `blocksFor` is
  // a nested function declaration, and TS does not carry a guard's narrowing
  // of `record` across that boundary the way it would for an inline callback.
  const currentRecord = record
  const title = String(currentRecord[schema.label_field] ?? currentRecord.id)
  const roleSummary = (currentRecord.role_summary as RoleSummaryEntry[] | undefined) ?? []
  const currentRoleNames = new Set(roleSummary.map((r) => r.role))
  const eligibleBlocks = eligibleProfileBlocks(schema.profile_blocks, currentRoleNames, currentRecord)

  function blocksFor(region: 'left' | 'center' | 'right') {
    return eligibleBlocks
      .filter((block) => block.region === region)
      .map((block) => {
        const Component = getProfileBlock(block.key)
        // No registered component for this key (module bundle not loaded,
        // or not built yet) -- fail-safe empty, matching the same "hidden,
        // not broken" contract an unreadable role/entity already gets.
        if (!Component) return null
        return <Component key={block.key} entity={entity} record={currentRecord} related={related} />
      })
  }

  return (
    <div className="crm-record-page">
      <div className="crm-record-page-header">
        <Link to={`/e/${entity}`} className="crm-record-page-back">
          ← {schema.label}
        </Link>
        <h1>{title}</h1>
        {roleSummary.length > 0 && (
          <div className="crm-role-pill-row">
            {roleSummary.map((r) => (
              <span key={r.role} className="crm-role-pill">
                {r.label}
              </span>
            ))}
          </div>
        )}
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
          {blocksFor('left')}
        </div>

        <div className="crm-record-page-center">
          <div className="crm-record-page-center-tabs">
            <button
              className={centerTab === 'timeline' ? 'crm-view-pill crm-view-pill-active' : 'crm-view-pill'}
              onClick={() => setCenterTab('timeline')}
            >
              Timeline
            </button>
            <button
              className={centerTab === 'records' ? 'crm-view-pill crm-view-pill-active' : 'crm-view-pill'}
              onClick={() => setCenterTab('records')}
            >
              Records
            </button>
          </div>
          {centerTab === 'timeline' ? (
            <Timeline entity={entity} recordId={recordId} />
          ) : (
            <ChildrenPanel entity={entity} recordId={recordId} />
          )}
          {blocksFor('center')}
        </div>

        <div className="crm-record-page-right">
          <div className="crm-record-detail-related-header">
            <h3>Related</h3>
            <LinkRecordControl entity={entity} recordId={recordId} onLinked={loadRelated} />
          </div>
          <RelatedPanel related={related} onChanged={loadRelated} />
          {blocksFor('right')}
        </div>
      </div>
    </div>
  )
}

export function RecordPage() {
  const { entity, recordId } = useParams<{ entity: string; recordId: string }>()
  return <RecordPageForRecord key={`${entity}:${recordId}`} />
}
