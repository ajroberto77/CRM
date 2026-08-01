import type { FieldKind } from '../records/types'

/** The one formatter per value kind (R5) -- every cell and detail field
 * renders through this, keyed by the field's `kind` from the schema, never
 * by a per-entity special case. */
export function formatValue(kind: FieldKind, value: unknown): string {
  if (value === null || value === undefined || value === '') return ''

  switch (kind) {
    case 'boolean':
      return value ? 'Yes' : 'No'
    case 'date':
      return formatDate(String(value))
    case 'datetime':
      return formatDateTime(String(value))
    case 'currency':
      return formatCurrency(value)
    case 'number':
      return typeof value === 'number' ? value.toLocaleString() : String(value)
    case 'jsonb':
      return typeof value === 'string' ? value : JSON.stringify(value)
    default:
      return String(value)
  }
}

export function formatDate(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  })
}

export function formatCurrency(value: unknown): string {
  const n = typeof value === 'number' ? value : Number(value)
  if (Number.isNaN(n)) return String(value)
  return n.toLocaleString(undefined, { style: 'currency', currency: 'USD' })
}

/** Best-effort label for an entity/record when only an id is on hand -- used
 * by the related panel before the far side is hydrated. */
export function recordLabel(record: Record<string, unknown>, labelField: string): string {
  const value = record[labelField]
  return typeof value === 'string' && value ? value : String(record.id ?? '')
}
