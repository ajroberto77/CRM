import { Link } from 'react-router-dom'
import { formatCurrency, formatValue } from '../lib/format'
import type { FieldKind } from './types'

interface FieldValueProps {
  kind: FieldKind
  value: unknown
  /** Only relevant for `kind === 'currency'` -- the row's own currency
   * code, so a value renders in the currency it was actually recorded in
   * rather than a hardcoded default. */
  currency?: string
  /** Only relevant for `kind === 'uuid'` -- the target entity to link to,
   * and its resolved human label (from `useRecordLabels`), when known. */
  referenceEntity?: string | null
  referenceLabel?: string | null
}

/** The one renderer for a field's READ-mode value (R1) -- `FieldInput.tsx`'s
 * counterpart for reading rather than writing: a table cell, a detail-view
 * field, and a related-block item all render a value through this, so a
 * reference/url/email/phone never gets a second, differently-wired
 * treatment depending on which screen happens to show it.
 *
 * Every anchor stops propagation on click -- these render inside clickable
 * table rows and detail-field containers, and following a link should never
 * ALSO trigger the row's own "open this record"/"start editing" handler.
 */
export function FieldValue({ kind, value, currency, referenceEntity, referenceLabel }: FieldValueProps) {
  if (value === null || value === undefined || value === '') return null

  if (kind === 'uuid' && referenceEntity) {
    const id = String(value)
    return (
      <Link
        className="crm-reference-link"
        to={`/r/${referenceEntity}/${id}`}
        onClick={(e) => e.stopPropagation()}
      >
        {referenceLabel || id}
      </Link>
    )
  }

  if (kind === 'url') {
    const href = String(value)
    return (
      <a
        className="crm-value-link"
        href={href}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
      >
        {href}
      </a>
    )
  }

  if (kind === 'email') {
    return (
      <a className="crm-value-link" href={`mailto:${value}`} onClick={(e) => e.stopPropagation()}>
        {String(value)}
      </a>
    )
  }

  if (kind === 'phone') {
    return (
      <a className="crm-value-link" href={`tel:${value}`} onClick={(e) => e.stopPropagation()}>
        {String(value)}
      </a>
    )
  }

  if (kind === 'currency') {
    return <>{formatCurrency(value, currency)}</>
  }

  return <>{formatValue(kind, value)}</>
}
