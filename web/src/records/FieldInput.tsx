import { useEffect, useState } from 'react'
import type { FocusEvent, KeyboardEvent } from 'react'
import { RecordPicker } from './RecordPicker'
import type { FieldKind } from './types'

interface FieldInputProps {
  kind: FieldKind
  value: unknown
  options: string[]
  onChange: (value: unknown) => void
  autoFocus?: boolean
  onBlur?: () => void
  onKeyDown?: (e: React.KeyboardEvent) => void
  /** For `kind === 'uuid'` with a fixed target (`FieldSpec.references`) --
   * renders a `RecordPicker` instead of a raw text box. A polymorphic
   * reference (`references_type_field`) still falls through to the plain
   * uuid text input below; picking both a type and a target is Phase 5's
   * concern (the record page's own subject editor), not this generic form. */
  references?: string | null
}

/** Whether an inline editor (RecordTable.saveCell, RecordDetail.saveField)
 * should close itself right after a commit. True for every single-decision
 * control (a select choice, a typed value on blur) -- false for multiselect,
 * whose chip/tag picker expects to stay open across several toggles and
 * closes only via its own container-blur (see MultiselectInput below). Kept
 * next to FieldInput itself, which already owns "the one control per field
 * kind," rather than duplicated as an inline `kind !== 'multiselect'` check
 * at each call site. */
export function shouldAutoCloseOnCommit(kind: FieldKind): boolean {
  return kind !== 'multiselect'
}

/** The one control per field kind (R1/R5) -- used for inline table-cell
 * editing, the detail-view form, and the create form alike, so a field never
 * renders differently depending on which screen it happens to be on. */
export function FieldInput({
  kind, value, options, onChange, autoFocus, onBlur, onKeyDown, references,
}: FieldInputProps) {
  if (kind === 'uuid' && references) {
    return (
      <RecordPicker
        entity={references}
        value={typeof value === 'string' ? value : null}
        onChange={onChange}
        autoFocus={autoFocus}
        onBlur={onBlur}
        onKeyDown={onKeyDown}
      />
    )
  }

  if (kind === 'boolean') {
    return (
      <input
        type="checkbox"
        checked={Boolean(value)}
        onChange={(e) => onChange(e.target.checked)}
        autoFocus={autoFocus}
        onBlur={onBlur}
      />
    )
  }

  if (kind === 'select' && options.length > 0) {
    return (
      <select
        value={typeof value === 'string' ? value : ''}
        onChange={(e) => onChange(e.target.value || null)}
        autoFocus={autoFocus}
        onBlur={onBlur}
        onKeyDown={onKeyDown}
      >
        <option value="" />
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    )
  }

  if (kind === 'multiselect') {
    // A real picker, not a JSON textarea -- multiselect values are already
    // plain arrays (the jsonb column round-trips as JSON, not a string), so
    // there is nothing to parse/stringify here, unlike the jsonb case below.
    return (
      <MultiselectInput
        value={Array.isArray(value) ? value.map(String) : []}
        options={options}
        onChange={onChange}
        autoFocus={autoFocus}
        onBlur={onBlur}
      />
    )
  }

  if (kind === 'jsonb') {
    return (
      <textarea
        value={typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)}
        onChange={(e) => onChange(e.target.value)}
        autoFocus={autoFocus}
        onBlur={onBlur}
        rows={4}
      />
    )
  }

  return (
    <TextLikeInput
      kind={kind}
      value={value}
      onChange={onChange}
      autoFocus={autoFocus}
      onBlur={onBlur}
      onKeyDown={onKeyDown}
    />
  )
}

interface TextLikeInputProps {
  kind: FieldKind
  value: unknown
  onChange: (value: unknown) => void
  autoFocus?: boolean
  onBlur?: () => void
  onKeyDown?: (e: React.KeyboardEvent) => void
}

/** Every non-discrete-choice kind (text/number/date/datetime/email/url/
 * phone) -- a single native `<input>`, but committing (calling `onChange`,
 * which is what triggers the PATCH at every call site) only on blur or
 * Enter, not on every keystroke. Holds its own draft state so typing stays
 * purely local until then; without this, a five-character edit was five
 * separate PATCH requests, the last one to land winning regardless of which
 * keystroke it corresponded to. */
function TextLikeInput({ kind, value, onChange, autoFocus, onBlur, onKeyDown }: TextLikeInputProps) {
  const inputType =
    kind === 'number' || kind === 'currency'
      ? 'number'
      : kind === 'date'
        ? 'date'
        : kind === 'datetime'
          ? 'datetime-local'
          : kind === 'email'
            ? 'email'
            : kind === 'url'
              ? 'url'
              : kind === 'phone'
                ? 'tel'
                : 'text'

  const [draft, setDraft] = useState(value === null || value === undefined ? '' : String(value))

  // The committed value can change out from under this control (a parent
  // re-fetch after another edit, e.g. RecordTable's optimistic row swap) --
  // resync the draft when that happens rather than showing a stale edit.
  useEffect(() => {
    setDraft(value === null || value === undefined ? '' : String(value))
  }, [value])

  function commit() {
    if (inputType === 'number') {
      onChange(draft === '' ? null : Number(draft))
    } else {
      onChange(draft === '' ? null : draft)
    }
  }

  return (
    <input
      type={inputType}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      autoFocus={autoFocus}
      onBlur={() => {
        commit()
        onBlur?.()
      }}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          commit()
        }
        onKeyDown?.(e)
      }}
    />
  )
}

interface MultiselectInputProps {
  value: string[]
  options: string[]
  onChange: (value: unknown) => void
  autoFocus?: boolean
  onBlur?: () => void
}

/** Closed-vocabulary fields (options present -- e.g. investor_mandate's
 * asset_classes/stages) render as toggleable chips; free-text multiselects
 * (options empty -- e.g. sectors/geographies/esg_constraints) render as a
 * tag input. Either way this replaces a raw JSON textarea, which was the
 * only widget every multiselect field had before this.
 *
 * Fires onChange on every toggle/add/remove (matching every other
 * FieldInput control, which commits per-change) but does NOT call onBlur
 * itself -- the caller's onBlur only fires when focus truly leaves the
 * whole widget, not after a single chip click, so a user can toggle
 * several values before the editor closes. */
function MultiselectInput({ value, options, onChange, autoFocus, onBlur }: MultiselectInputProps) {
  const [draft, setDraft] = useState('')

  function toggle(option: string) {
    onChange(value.includes(option) ? value.filter((v) => v !== option) : [...value, option])
  }

  function removeTag(tag: string) {
    onChange(value.filter((v) => v !== tag))
  }

  function addTag(raw: string) {
    const tag = raw.trim()
    if (!tag || value.includes(tag)) return
    onChange([...value, tag])
    setDraft('')
  }

  function handleContainerBlur(e: FocusEvent<HTMLDivElement>) {
    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
      onBlur?.()
    }
  }

  if (options.length > 0) {
    return (
      <div className="crm-multiselect" onBlur={handleContainerBlur}>
        {options.map((o, i) => (
          <button
            key={o}
            type="button"
            className={
              value.includes(o) ? 'crm-multiselect-chip crm-multiselect-chip-selected' : 'crm-multiselect-chip'
            }
            onClick={() => toggle(o)}
            autoFocus={autoFocus && i === 0}
          >
            {o}
          </button>
        ))}
      </div>
    )
  }

  return (
    <div className="crm-multiselect" onBlur={handleContainerBlur}>
      {value.map((tag) => (
        <span key={tag} className="crm-multiselect-chip crm-multiselect-chip-selected">
          {tag}
          <button
            type="button"
            className="crm-multiselect-chip-remove"
            onClick={() => removeTag(tag)}
            aria-label={`Remove ${tag}`}
          >
            ×
          </button>
        </span>
      ))}
      <input
        className="crm-multiselect-taginput"
        type="text"
        value={draft}
        placeholder="Add…"
        autoFocus={autoFocus}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault()
            addTag(draft)
          } else if (e.key === 'Backspace' && draft === '' && value.length > 0) {
            removeTag(value[value.length - 1])
          }
        }}
      />
    </div>
  )
}
