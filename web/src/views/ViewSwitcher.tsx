import { useMemo, useState } from 'react'
import type { EntitySchema, SavedView } from '../records/types'

interface ViewSwitcherProps {
  views: SavedView[]
  activeViewId: string | null
  schema: EntitySchema
  onSelect: (view: SavedView | null) => void
  onSaveCurrent: (name: string, kind: SavedView['kind'], groupBy: string | null) => void
  onDelete: (id: string) => void
}

/** Fields a board can group by -- writable core fields of a kind that can
 * hold a single lane-shaped value. Excludes jsonb/multiselect (not
 * lane-shaped), booleans (only 2 lanes, arguably not worth a board), and
 * custom fields (v1 keeps grouping to core fields; not a hard limitation,
 * just not built yet). */
function groupableFields(schema: EntitySchema): string[] {
  return Object.entries(schema.fields)
    .filter(([, f]) => f.writable && (f.kind === 'select' || f.kind === 'text'))
    .map(([name]) => name)
}

export function ViewSwitcher({ views, activeViewId, schema, onSelect, onSaveCurrent, onDelete }: ViewSwitcherProps) {
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<SavedView['kind']>('table')
  const [groupBy, setGroupBy] = useState('')
  const candidates = useMemo(() => groupableFields(schema), [schema])

  return (
    <div className="crm-view-switcher">
      <button className={activeViewId === null ? 'crm-view-pill crm-view-pill-active' : 'crm-view-pill'} onClick={() => onSelect(null)}>
        All
      </button>
      {views.map((v) => (
        <span key={v.id} className="crm-view-pill-group">
          <button
            className={activeViewId === v.id ? 'crm-view-pill crm-view-pill-active' : 'crm-view-pill'}
            onClick={() => onSelect(v)}
            title={v.kind === 'board' ? `Board, grouped by ${v.group_by}` : undefined}
          >
            {v.kind === 'board' && <span className="crm-view-pill-board-icon">▦ </span>}
            {v.name}
          </button>
          {activeViewId === v.id && (
            <button className="crm-view-pill-remove" title="Delete view" onClick={() => onDelete(v.id)}>
              ×
            </button>
          )}
        </span>
      ))}

      {naming ? (
        <form
          className="crm-view-save-form"
          onSubmit={(e) => {
            e.preventDefault()
            if (!name.trim()) return
            onSaveCurrent(name.trim(), kind, kind === 'board' ? groupBy || null : null)
            setName('')
            setKind('table')
            setGroupBy('')
            setNaming(false)
          }}
        >
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="View name" />
          <select aria-label="View type" value={kind} onChange={(e) => setKind(e.target.value as SavedView['kind'])}>
            <option value="table">Table</option>
            <option value="board">Board</option>
          </select>
          {kind === 'board' && (
            <select aria-label="Group by" value={groupBy} onChange={(e) => setGroupBy(e.target.value)} required>
              <option value="" disabled>
                Group by…
              </option>
              {candidates.map((f) => (
                <option key={f} value={f}>
                  {f.replace(/_/g, ' ')}
                </option>
              ))}
            </select>
          )}
          <button type="submit">Save</button>
          <button type="button" onClick={() => setNaming(false)}>
            Cancel
          </button>
        </form>
      ) : (
        <button className="crm-view-pill crm-view-pill-new" onClick={() => setNaming(true)}>
          + Save view
        </button>
      )}
    </div>
  )
}
