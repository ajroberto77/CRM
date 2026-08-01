import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useEntitySchema } from './useEntitySchema'
import { RecordTable } from './RecordTable'
import { RecordDetail } from './RecordDetail'
import { CreateRecordModal } from './CreateRecordModal'
import { ViewSwitcher } from '../views/ViewSwitcher'
import { useSavedViews } from '../views/useSavedViews'
import type { FilterNode, SavedView } from './types'

/** Keyed by `entity` in the export below so switching entities fully remounts
 * this component -- state set inside a `useEffect` always lags one render
 * behind the prop change that triggered it, so without a remount this page
 * would render one frame with the NEW entity but the PREVIOUS entity's
 * schema (wrong default_sort field, wrong columns) before the effect catches
 * up. A remount makes that render impossible rather than racing it. */
function EntityListPageForEntity() {
  const { entity = '', recordId } = useParams<{ entity: string; recordId?: string }>()
  const navigate = useNavigate()
  const { schema, loading, error } = useEntitySchema(entity)
  const { views, canUseViews, saveView, deleteView } = useSavedViews(entity)

  const [activeView, setActiveView] = useState<SavedView | null>(null)
  const [search, setSearch] = useState('')
  const [creating, setCreating] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)

  const filters = useMemo<FilterNode | null>(() => {
    const clauses: FilterNode[] = []
    if (activeView?.filters) clauses.push(activeView.filters)
    if (search.trim() && schema) {
      const searchable = schema.searchable.length > 0 ? schema.searchable : [schema.label_field]
      clauses.push({ or: searchable.map((field) => ({ field, op: 'contains', value: search.trim() })) })
    }
    if (clauses.length === 0) return null
    return clauses.length === 1 ? clauses[0] : { and: clauses }
  }, [activeView, search, schema])

  if (loading) return <div className="crm-table-status">Loading…</div>
  if (error || !schema) return <div className="crm-table-status crm-table-status-error">{error ?? 'Unknown entity'}</div>

  return (
    <div className="crm-entity-page">
      <div className="crm-entity-page-header">
        <h1>{schema.label}</h1>
        {schema.can_create && (
          <button className="crm-primary" onClick={() => setCreating(true)}>
            + New
          </button>
        )}
      </div>

      <div className="crm-entity-page-toolbar">
        <input
          className="crm-search-box"
          placeholder={`Search ${schema.label.toLowerCase()}…`}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {canUseViews && <ViewSwitcher
          views={views}
          activeViewId={activeView?.id ?? null}
          onSelect={setActiveView}
          onSaveCurrent={(name) => saveView(name, activeView?.filters ?? null, activeView?.sort ?? null, activeView?.columns ?? null)}
          onDelete={(id) => {
            deleteView(id)
            if (activeView?.id === id) setActiveView(null)
          }}
        />}
      </div>

      <div className="crm-entity-page-body">
        <div className={recordId ? 'crm-entity-page-table crm-entity-page-table-split' : 'crm-entity-page-table'}>
          <RecordTable
            entity={entity}
            schema={schema}
            filters={filters}
            sort={activeView?.sort ?? null}
            columns={activeView?.columns ?? null}
            onOpenRecord={(id) => navigate(`/e/${entity}/${id}`)}
            refreshToken={refreshToken}
          />
        </div>

        {recordId && (
          <div className="crm-entity-page-detail">
            <RecordDetail
              entity={entity}
              recordId={recordId}
              schema={schema}
              onClose={() => navigate(`/e/${entity}`)}
              onDeleted={() => {
                setRefreshToken((t) => t + 1)
                navigate(`/e/${entity}`)
              }}
            />
          </div>
        )}
      </div>

      {creating && (
        <CreateRecordModal
          entity={entity}
          schema={schema}
          onClose={() => setCreating(false)}
          onCreated={(record) => {
            setCreating(false)
            setRefreshToken((t) => t + 1)
            navigate(`/e/${entity}/${record.id}`)
          }}
        />
      )}
    </div>
  )
}

export function EntityListPage() {
  const { entity } = useParams<{ entity: string }>()
  return <EntityListPageForEntity key={entity} />
}
