import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiPost } from '../lib/api'
import { fieldLabel, formatValue } from '../lib/format'
import { FieldInput, shouldAutoCloseOnCommit } from './FieldInput'
import { FieldValue } from './FieldValue'
import { useRecordList } from './useRecordList'
import { useRecordLabels } from './useRecordLabels'
import { useSaveField } from './useSaveField'
import type { EntitySchema, FieldKind, FilterNode, RecordRow, SortSpec } from './types'

interface RecordTableProps {
  entity: string
  schema: EntitySchema
  filters: FilterNode | null
  sort: SortSpec[] | null
  columns: string[] | null
  refreshToken: number
}

const SYSTEM_COLUMNS = new Set(['id', 'owner_id'])

function defaultColumns(schema: EntitySchema): string[] {
  const cols = [schema.label_field]
  for (const name of Object.keys(schema.fields)) {
    if (name === schema.label_field || SYSTEM_COLUMNS.has(name)) continue
    if (name === 'created_at') continue
    cols.push(name)
  }
  return cols
}

export function RecordTable({ entity, schema, filters, sort, columns, refreshToken }: RecordTableProps) {
  const [editingCell, setEditingCell] = useState<{ id: string; field: string } | null>(null)

  // A saved view's explicit `columns` wins; otherwise the registry's own
  // curated `list_columns` (Phase 0) beats the auto-derived "every field"
  // fallback, which is the wrong table for an entity with a jsonb blob or a
  // pile of uuid FKs and no curated list yet.
  //
  // `columns && columns.length > 0`, not a bare `columns ??` -- a freshly
  // saved view whose columns were never explicitly chosen gets `columns: []`
  // back from the API, not `null` (core.saved_views.columns is `NOT NULL
  // DEFAULT '[]'::jsonb`, so omitting the field on create still reads back
  // as an empty array). `??` only falls back on null/undefined, so an empty
  // array from a real, just-saved view used to render a table with zero
  // columns and zero rows worth of content, not the derived default this
  // fallback exists to provide.
  const visibleColumns = useMemo(
    () =>
      columns && columns.length > 0
        ? columns
        : schema.list_columns.length > 0
          ? schema.list_columns
          : defaultColumns(schema),
    [columns, schema],
  )
  const effectiveSort = useMemo(
    () => (sort && sort.length > 0 ? sort : schema.default_sort),
    [sort, schema],
  )

  const { result, loading, error, setResult } = useRecordList(entity, filters, effectiveSort, refreshToken)

  const { saveField, saving: savingCell } = useSaveField({
    entity,
    schema,
    onSaved: (recordId, updated) =>
      setResult((prev) =>
        prev ? { ...prev, records: prev.records.map((r) => (r.id === recordId ? updated : r)) } : prev,
      ),
    onSettled: (_, __, kind) => {
      if (shouldAutoCloseOnCommit(kind)) setEditingCell(null)
    },
  })

  function fieldSchemaFor(name: string) {
    const core = schema.fields[name]
    if (core) {
      return {
        kind: core.kind, options: core.options, writable: core.writable,
        label: core.label, references: core.references,
      }
    }
    const custom = schema.custom_fields.find((c) => c.key === name)
    if (custom) {
      return {
        kind: custom.kind, options: custom.options, writable: custom.writable,
        label: custom.label, references: null,
      }
    }
    return { kind: 'text' as FieldKind, options: [] as string[], writable: false, label: name, references: null }
  }

  function cellValue(row: RecordRow, name: string): unknown {
    if (schema.fields[name]) return row[name]
    return row.custom?.[name]
  }

  const referenceRefs = useMemo(() => {
    if (!result) return []
    return visibleColumns.flatMap((name) => {
      const references = schema.fields[name]?.references
      if (!references) return []
      return result.records.map((row) => ({ entity: references, id: cellValue(row, name) as string | null }))
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result, visibleColumns, schema])
  const { labelFor } = useRecordLabels(referenceRefs)

  if (loading) return <div className="crm-table-status">Loading…</div>
  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (!result || result.records.length === 0) return <div className="crm-table-status">No records.</div>

  return (
    <div className="crm-record-table-wrap">
      <table className="crm-record-table" data-density="compact">
        <thead>
          <tr>
            {visibleColumns.map((name) => (
              <th key={name}>{fieldLabel(fieldSchemaFor(name), name)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.records.map((row) => (
            <tr key={row.id}>
              {visibleColumns.map((name) => {
                const { kind, options, writable, references } = fieldSchemaFor(name)
                const isEditing = editingCell?.id === row.id && editingCell?.field === name
                const value = cellValue(row, name)
                const isLabelColumn = name === schema.label_field
                return (
                  <td key={name}>
                    {isLabelColumn ? (
                      <Link className="crm-record-link" to={`/e/${entity}/${row.id}`}>
                        {formatValue(kind, value) || '(untitled)'}
                      </Link>
                    ) : isEditing ? (
                      <FieldInput
                        kind={kind}
                        value={value}
                        options={options}
                        references={references}
                        autoFocus
                        onChange={(v) => saveField(String(row.id), name, v, kind)}
                        onBlur={() => setEditingCell(null)}
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') setEditingCell(null)
                        }}
                      />
                    ) : (
                      <span
                        className={writable ? 'crm-cell-editable' : undefined}
                        onClick={() => writable && !savingCell && setEditingCell({ id: String(row.id), field: name })}
                      >
                        <FieldValue
                          kind={kind}
                          value={value}
                          currency={row.currency as string | undefined}
                          referenceEntity={references}
                          referenceLabel={references ? labelFor(references, value as string | null) : null}
                        />
                      </span>
                    )}
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="crm-table-footer">{result.total} record{result.total === 1 ? '' : 's'}</div>
    </div>
  )
}

export async function createRecord(entity: string, values: Record<string, unknown>): Promise<RecordRow> {
  const res = await apiPost<{ record: RecordRow }>(`/records/${entity}`, values)
  return res.record
}
