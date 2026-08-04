import type { FieldKind } from '../records/types'

/** The one formatter per value kind (R5) -- every cell and detail field
 * renders through this, keyed by the field's `kind` from the schema, never
 * by a per-entity special case. Returns plain text; `FieldValue.tsx` is the
 * counterpart that wraps this for the kinds (`uuid` reference, `url`,
 * `email`, `phone`) that render as a link instead. */
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
    case 'multiselect':
      return Array.isArray(value) ? value.join(', ') : String(value)
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

/** `currency` defaults to 'USD' only for a caller with no currency of its
 * own to pass -- every entity with a `currency`-kind field also carries a
 * sibling `currency` text column (server/core/registry.py's `deal`,
 * `modules/funds`' `fund`/`commitment`, `modules/investor_portal`'s
 * `investment_pathway`/`investor_mandate`), so a real caller should read
 * that field off the same row rather than accept the default. */
export function formatCurrency(value: unknown, currency = 'USD'): string {
  const n = typeof value === 'number' ? value : Number(value)
  if (Number.isNaN(n)) return String(value)
  try {
    return n.toLocaleString(undefined, { style: 'currency', currency })
  } catch {
    // An invalid/unrecognized ISO code (bad data, not a bug worth crashing
    // the cell over) -- render the number rather than let Intl throw.
    return n.toLocaleString(undefined, { style: 'currency', currency: 'USD' })
  }
}

/** Best-effort human label for a record. Prefers `labelField` (the
 * entity's own `EntitySchema.label_field`) when given and present; falls
 * back across the common label-ish column names otherwise, for a related
 * record whose target entity's schema the caller does not have on hand
 * (`server/core/associations.py`'s `related_blocks()` payload does not
 * carry it). The one label-guessing implementation (R1) -- previously
 * duplicated as `RecordDetail.tsx`'s `relatedRecordLabel` and
 * `LinkRecordControl.tsx`'s inline `r[schema?.label_field ?? 'id']`. */
const FALLBACK_LABEL_FIELDS = ['name', 'full_name', 'title', 'filename', 'pattern', 'body']

export function recordLabel(
  record: Record<string, unknown> | null | undefined,
  labelField?: string,
): string {
  if (!record) return ''
  if (labelField) {
    const value = record[labelField]
    if (value !== null && value !== undefined && value !== '') return String(value)
  }
  for (const field of FALLBACK_LABEL_FIELDS) {
    const value = record[field]
    if (typeof value === 'string' && value) return value
  }
  return String(record.id ?? '')
}

/** A field's human label -- the registry's own `FieldSpec.label` when the
 * backend supplied one, else a humanized field name. The one implementation
 * (R1) of what was four independent `name.replace(/_/g, ' ')` call sites. */
export function fieldLabel(field: { label?: string } | undefined, name: string): string {
  return (field?.label && field.label.trim()) || name.replace(/_/g, ' ')
}
