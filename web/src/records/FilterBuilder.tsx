import { useMemo } from 'react'
import { fieldLabel } from '../lib/format'
import { FieldInput } from './FieldInput'
import type { EntitySchema, FieldKind, FilterNode } from './types'

export interface FilterClause {
  id: string
  field: string
  op: string
  value: unknown
}

interface FilterableField {
  ref: string
  label: string
  kind: FieldKind
  options: string[]
  references: string | null
  operators: string[]
}

// Only single-value operators -- "in"/"between" need a different (multi-
// value) input shape, and "is_null"/"is_not_null" need none at all. All
// three are legitimate extensions of this same picker later, not excluded
// for any semantic reason -- just not built yet.
const SUPPORTED_OPS = new Set(['eq', 'neq', 'contains', 'starts_with', 'gt', 'gte', 'lt', 'lte'])

const OP_LABELS: Record<string, string> = {
  eq: 'is', neq: 'is not', contains: 'contains', starts_with: 'starts with',
  gt: '>', gte: '≥', lt: '<', lte: '≤',
}

// Excluded from the field picker entirely, matching RecordTable.tsx's own
// SYSTEM_COLUMNS precedent for the same reason: `id` is rarely something
// worth filtering by hand, and `owner_id` has no FieldSpec.references (see
// registry.py's spine()), so FieldInput renders it as a bare uuid text box
// -- a dead end, not a real filter control.
const SYSTEM_FIELDS = new Set(['id', 'owner_id'])

/** Every field this entity can filter on through the builder's supported
 * operator subset -- core fields and custom fields alike, straight from
 * the schema's own `operators` (server/core/query.py's `operators_for()`,
 * R1: never a second per-kind table hand-maintained here). Multiselect and
 * jsonb are excluded: their only real operator (`contains`, a jsonb
 * containment test) needs a value shape (a JSON array) this generic
 * single-value picker doesn't build yet.
 *
 * Ordered with the entity's own curated `list_columns` first (same
 * precedent RecordTable.tsx's `visibleColumns` already established --
 * registry.py's `spine()` always inserts id/owner_id/created_at/updated_at
 * ahead of every business field in raw declaration order, so without this
 * every entity's dropdown would read "Created at, Updated at, Name,
 * Status, ..." with the fields anyone actually wants to filter by pushed
 * to the bottom). Anything not in `list_columns` keeps its original
 * (stable-sorted) order after that. */
function filterableFields(schema: EntitySchema): FilterableField[] {
  const core: FilterableField[] = Object.entries(schema.fields)
    .filter(
      ([name, f]) =>
        !SYSTEM_FIELDS.has(name) && f.kind !== 'multiselect' && f.kind !== 'jsonb' &&
        f.operators.some((op) => SUPPORTED_OPS.has(op)),
    )
    .map(([name, f]) => ({
      ref: name, label: fieldLabel(f, name), kind: f.kind, options: f.options,
      references: f.references, operators: f.operators,
    }))
  const custom: FilterableField[] = schema.custom_fields
    .filter((cf) => cf.kind !== 'multiselect' && cf.kind !== 'jsonb' && cf.operators.some((op) => SUPPORTED_OPS.has(op)))
    .map((cf) => ({
      ref: `custom.${cf.key}`, label: cf.label || cf.key, kind: cf.kind,
      options: cf.options, references: null, operators: cf.operators,
    }))
  const all = [...core, ...custom]
  const priority = new Map(schema.list_columns.map((name, i) => [name, i]))
  return all
    .map((field, i) => ({ field, i }))
    .sort((a, b) => {
      const pa = priority.get(a.field.ref) ?? Infinity
      const pb = priority.get(b.field.ref) ?? Infinity
      if (pa !== pb) return pa - pb
      return a.i - b.i // stable: preserve original order among ties
    })
    .map(({ field }) => field)
}

interface FilterBuilderProps {
  schema: EntitySchema
  clauses: FilterClause[]
  onChange: (clauses: FilterClause[]) => void
}

/** Ad hoc filter rows layered on top of the search box and whichever saved
 * view is active -- field, operator, value, one row per clause, ANDed
 * together. Reuses `FieldInput` for the value control (R1): a reference
 * field gets the same `RecordPicker` search-then-pick it gets everywhere
 * else, a `select` field gets the same options dropdown, rather than this
 * component inventing a second value-widget-per-kind table. */
export function FilterBuilder({ schema, clauses, onChange }: FilterBuilderProps) {
  const candidates = useMemo(() => filterableFields(schema), [schema])

  if (candidates.length === 0) return null

  function addClause() {
    const first = candidates[0]
    const op = first.operators.find((o) => SUPPORTED_OPS.has(o)) ?? 'eq'
    onChange([...clauses, { id: crypto.randomUUID(), field: first.ref, op, value: null }])
  }

  function updateClause(id: string, changes: Partial<FilterClause>) {
    onChange(clauses.map((c) => (c.id === id ? { ...c, ...changes } : c)))
  }

  function removeClause(id: string) {
    onChange(clauses.filter((c) => c.id !== id))
  }

  return (
    <div className="crm-filter-builder">
      {clauses.map((clause) => {
        const field = candidates.find((c) => c.ref === clause.field) ?? candidates[0]
        const ops = field.operators.filter((o) => SUPPORTED_OPS.has(o))
        return (
          <div className="crm-filter-row" key={clause.id}>
            <select
              aria-label="Field"
              value={clause.field}
              onChange={(e) => {
                const next = candidates.find((c) => c.ref === e.target.value) ?? field
                const nextOps = next.operators.filter((o) => SUPPORTED_OPS.has(o))
                updateClause(clause.id, { field: next.ref, op: nextOps[0] ?? 'eq', value: null })
              }}
            >
              {candidates.map((c) => (
                <option key={c.ref} value={c.ref}>
                  {c.label}
                </option>
              ))}
            </select>
            <select aria-label="Operator" value={clause.op} onChange={(e) => updateClause(clause.id, { op: e.target.value })}>
              {ops.map((o) => (
                <option key={o} value={o}>
                  {OP_LABELS[o] ?? o}
                </option>
              ))}
            </select>
            <div className="crm-filter-row-value">
              <FieldInput
                kind={field.kind}
                value={clause.value}
                options={field.options}
                references={field.references}
                onChange={(v) => updateClause(clause.id, { value: v })}
              />
            </div>
            <button
              type="button"
              className="crm-filter-row-remove"
              aria-label="Remove filter"
              onClick={() => removeClause(clause.id)}
            >
              ×
            </button>
          </div>
        )
      })}
      <button type="button" className="crm-filter-add" onClick={addClause}>
        + Add filter
      </button>
    </div>
  )
}

/** Compile the builder's flat clause list into one FilterNode (ANDed),
 * dropping any clause whose value is still unset -- an incomplete row
 * (a field/operator picked but no value typed yet) must not silently
 * filter the list down to nothing by comparing a real column against
 * null/empty. */
export function compileClauses(clauses: FilterClause[]): FilterNode | null {
  const complete = clauses.filter((c) => c.value !== null && c.value !== undefined && c.value !== '')
  if (complete.length === 0) return null
  const nodes: FilterNode[] = complete.map((c) => ({ field: c.field, op: c.op, value: c.value }))
  return nodes.length === 1 ? nodes[0] : { and: nodes }
}
