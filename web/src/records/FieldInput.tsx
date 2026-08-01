import type { FieldKind } from './types'

interface FieldInputProps {
  kind: FieldKind
  value: unknown
  options: string[]
  onChange: (value: unknown) => void
  autoFocus?: boolean
  onBlur?: () => void
  onKeyDown?: (e: React.KeyboardEvent) => void
}

/** The one control per field kind (R1/R5) -- used for inline table-cell
 * editing, the detail-view form, and the create form alike, so a field never
 * renders differently depending on which screen it happens to be on. */
export function FieldInput({ kind, value, options, onChange, autoFocus, onBlur, onKeyDown }: FieldInputProps) {
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

  if (kind === 'jsonb' || kind === 'multiselect') {
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

  return (
    <input
      type={inputType}
      value={value === null || value === undefined ? '' : String(value)}
      onChange={(e) => {
        const raw = e.target.value
        if (inputType === 'number') {
          onChange(raw === '' ? null : Number(raw))
        } else {
          onChange(raw === '' ? null : raw)
        }
      }}
      autoFocus={autoFocus}
      onBlur={onBlur}
      onKeyDown={onKeyDown}
    />
  )
}
