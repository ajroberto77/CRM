import { useState } from 'react'
import { useEntitySchema } from './useEntitySchema'
import { useRecordSearch } from './useRecordSearch'
import { useRecordLabels } from './useRecordLabels'
import { recordLabel } from '../lib/format'
import type { RecordRow } from './types'

interface RecordPickerProps {
  /** The target entity to search -- a `FieldSpec.references` value. */
  entity: string
  value: string | null
  onChange: (id: string | null) => void
  autoFocus?: boolean
  onBlur?: () => void
  onKeyDown?: (e: React.KeyboardEvent) => void
}

/** The reference-field control -- `FieldInput`'s counterpart for a `uuid`
 * field with a known target entity: search-then-pick instead of pasting a
 * uuid into a free-text box. The currently linked record's label resolves
 * through the same `useRecordLabels` cache every other reference render
 * uses, so an already-linked fund shows its name on mount, not a blank box.
 */
export function RecordPicker({ entity, value, onChange, autoFocus, onBlur, onKeyDown }: RecordPickerProps) {
  const { schema } = useEntitySchema(entity)
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState(false)
  const { results, loading, error } = useRecordSearch(entity, schema, query)
  const { labelFor } = useRecordLabels(value ? [{ entity, id: value }] : [])
  const selectedLabel = value ? labelFor(entity, value) : null

  // The selected record's own name, as real input text -- not a placeholder
  // (a placeholder disappears the instant the field is focused/typed into,
  // and renders in the browser's dimmed "hint" color, both wrong for a
  // value that is actually set). Once the user types, `query` takes over.
  const displayValue = query || (value ? selectedLabel ?? '' : '')

  function pick(record: RecordRow) {
    onChange(String(record.id))
    setQuery('')
    setOpen(false)
    onBlur?.()
  }

  function clear() {
    onChange(null)
    setQuery('')
  }

  return (
    <div
      className="crm-record-picker"
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          setOpen(false)
          onBlur?.()
        }
      }}
    >
      <div className="crm-record-picker-input-row">
        <input
          className="crm-record-picker-input"
          placeholder={`Search ${schema?.label.toLowerCase() ?? entity}…`}
          value={displayValue}
          autoFocus={autoFocus}
          onChange={(e) => {
            setQuery(e.target.value)
            setOpen(true)
          }}
          onFocus={(e) => {
            setOpen(true)
            // A click-then-type should replace the whole shown label, the
            // same convention every combobox-style picker uses -- without
            // this, typing would splice into the middle of the existing
            // record's name instead of starting a fresh search.
            e.target.select()
          }}
          onKeyDown={onKeyDown}
        />
        {value && (
          <button type="button" className="crm-record-picker-clear" aria-label="Clear" onClick={clear}>
            ×
          </button>
        )}
      </div>
      {open && query.trim() && (
        <div className="crm-record-picker-results">
          {loading && <div className="crm-record-picker-status">Searching…</div>}
          {error && <div className="crm-record-picker-status crm-record-picker-error">{error}</div>}
          {!loading && !error && results.length === 0 && (
            <div className="crm-record-picker-status">No matches.</div>
          )}
          {results.map((r) => (
            <button
              key={String(r.id)}
              type="button"
              className="crm-record-picker-result"
              onClick={() => pick(r)}
            >
              {recordLabel(r, schema?.label_field)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
