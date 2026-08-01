import { useState } from 'react'
import type { SavedView } from '../records/types'

interface ViewSwitcherProps {
  views: SavedView[]
  activeViewId: string | null
  onSelect: (view: SavedView | null) => void
  onSaveCurrent: (name: string) => void
  onDelete: (id: string) => void
}

export function ViewSwitcher({ views, activeViewId, onSelect, onSaveCurrent, onDelete }: ViewSwitcherProps) {
  const [naming, setNaming] = useState(false)
  const [name, setName] = useState('')

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
          >
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
            onSaveCurrent(name.trim())
            setName('')
            setNaming(false)
          }}
        >
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="View name" />
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
