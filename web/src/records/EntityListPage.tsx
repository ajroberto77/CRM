import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useEntitySchema } from './useEntitySchema'
import { RecordTable } from './RecordTable'
import { RecordBoard } from './RecordBoard'
import { RecordDetail } from './RecordDetail'
import { CreateRecordModal } from './CreateRecordModal'
import { buildSearchFilter } from './searchFilter'
import { FilterBuilder, compileClauses } from './FilterBuilder'
import type { FilterClause } from './FilterBuilder'
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
  const { entity: entityParam = '', recordId } = useParams<{ entity: string; recordId?: string }>()
  // Normalize entity name: convert kebab-case (from URL) to snake_case (entity name).
  // URLs use kebab-case by convention, but entity names use snake_case.
  const entity = entityParam.replace(/-/g, '_')
  const navigate = useNavigate()
  const { schema, loading, error } = useEntitySchema(entity)
  const { views, canUseViews, saveView, deleteView } = useSavedViews(entity)

  const [activeView, setActiveView] = useState<SavedView | null>(null)
  const [search, setSearch] = useState('')
  // Debounced separately from `search` itself -- the input stays instantly
  // responsive to typing, but the filter (and therefore the list fetch it
  // drives) only updates 250ms after the user stops, not once per keystroke.
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [filterClauses, setFilterClauses] = useState<FilterClause[]>([])
  const [showFilterBuilder, setShowFilterBuilder] = useState(false)
  const [creating, setCreating] = useState(false)
  const [refreshToken, setRefreshToken] = useState(0)

  useEffect(() => {
    const handle = setTimeout(() => setDebouncedSearch(search), 250)
    return () => clearTimeout(handle)
  }, [search])

  // The full set of filters currently narrowing the list -- the selected
  // view's own stored filter (opaque, applied as-is), the search box, and
  // whatever ad hoc rows the filter builder holds, all ANDed together.
  // "Save view" below saves exactly this, not a stale `activeView?.filters`
  // (the bug this phase fixes: a search term or builder clause the user is
  // actually looking at used to vanish from a saved view entirely).
  const filters = useMemo<FilterNode | null>(() => {
    const clauses: FilterNode[] = []
    if (activeView?.filters) clauses.push(activeView.filters)
    if (debouncedSearch.trim() && schema) {
      clauses.push(buildSearchFilter(schema, debouncedSearch.trim()))
    }
    const builderFilter = compileClauses(filterClauses)
    if (builderFilter) clauses.push(builderFilter)
    if (clauses.length === 0) return null
    return clauses.length === 1 ? clauses[0] : { and: clauses }
  }, [activeView, debouncedSearch, filterClauses, schema])

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
        <button
          className={filterClauses.length > 0 ? 'crm-filter-toggle crm-filter-toggle-active' : 'crm-filter-toggle'}
          onClick={() => setShowFilterBuilder((s) => !s)}
        >
          Filters{filterClauses.length > 0 ? ` (${filterClauses.length})` : ''}
        </button>
        {canUseViews && <ViewSwitcher
          views={views}
          activeViewId={activeView?.id ?? null}
          schema={schema}
          onSelect={setActiveView}
          onSaveCurrent={(name, kind, groupBy) =>
            // The actual, currently-applied filter (view + search + builder
            // clauses combined) -- not `activeView?.filters`, which is only
            // ever the PREVIOUSLY selected view's own stored filter and
            // silently drops whatever the user is actually looking at.
            saveView(name, filters, activeView?.sort ?? null, activeView?.columns ?? null, kind, groupBy)
          }
          onDelete={(id) => {
            deleteView(id)
            if (activeView?.id === id) setActiveView(null)
          }}
        />}
      </div>

      {showFilterBuilder && (
        <FilterBuilder schema={schema} clauses={filterClauses} onChange={setFilterClauses} />
      )}

      <div className="crm-entity-page-body">
        <div className={recordId ? 'crm-entity-page-table crm-entity-page-table-split' : 'crm-entity-page-table'}>
          {activeView?.kind === 'board' && activeView.group_by ? (
            <RecordBoard
              entity={entity}
              schema={schema}
              filters={filters}
              sort={activeView?.sort ?? null}
              groupBy={activeView.group_by}
              onOpenRecord={(id) => navigate(`/e/${entity}/${id}`)}
              refreshToken={refreshToken}
            />
          ) : (
            <RecordTable
              entity={entity}
              schema={schema}
              filters={filters}
              sort={activeView?.sort ?? null}
              columns={activeView?.columns ?? null}
              refreshToken={refreshToken}
            />
          )}
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
