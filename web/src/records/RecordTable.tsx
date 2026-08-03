import { useMemo, useState } from 'react'
import { apiPatch, apiPost, ApiError } from '../lib/api'
import { formatValue } from '../lib/format'
import { FieldInput } from './FieldInput'
import { useRecordList } from './useRecordList'
import type { EntitySchema, FilterNode, RecordRow, SortSpec } from './types'

interface RecordTableProps {
  entity: string
  schema: EntitySchema
  filters: FilterNode | null
  sort: SortSpec[] | null
  columns: string[] | null
  onOpenRecord: (id: string) => void
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

export function RecordTable({ entity, schema, filters, sort, columns, onOpenRecord, refreshToken }: RecordTableProps) {
  const [editingCell, setEditingCell] = useState<{ id: string; field: string } | null>(null)
  const [savingCell, setSavingCell] = useState(false)

  const visibleColumns = useMemo(() => columns ?? defaultColumns(schema), [columns, schema])
  const effectiveSort = useMemo(
    () => sort ?? schema.default_sort,
    [sort, schema],
  )

  const { result, loading, error, setResult } = useRecordList(entity, filters, effectiveSort, refreshToken)

  async function saveCell(row: RecordRow, field: string, value: unknown) {
    setSavingCell(true)
    try {
      const isCustom = schema.fields[field] === undefined
      const changes = isCustom ? { custom: { [field]: value } } : { [field]: value }
      const updated = await apiPatch<{ record: RecordRow }>(`/records/${entity}/${row.id}`, { changes })
      setResult((prev) =>
        prev
          ? { ...prev, records: prev.records.map((r) => (r.id === row.id ? updated.record : r)) }
          : prev,
      )
    } catch (err) {
      window.alert(err instanceof ApiError ? err.detail : 'Failed to save')
    } finally {
      setSavingCell(false)
      setEditingCell(null)
    }
  }

  function fieldSchemaFor(name: string) {
    const core = schema.fields[name]
    if (core) return { kind: core.kind, options: core.options, writable: core.writable }
    const custom = schema.custom_fields.find((c) => c.key === name)
    if (custom) return { kind: custom.kind, options: custom.options, writable: custom.writable }
    return { kind: 'text' as const, options: [] as string[], writable: false }
  }

  function cellValue(row: RecordRow, name: string): unknown {
    if (schema.fields[name]) return row[name]
    return row.custom?.[name]
  }

  if (loading) return <div className="crm-table-status">Loading…</div>
  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (!result || result.records.length === 0) return <div className="crm-table-status">No records.</div>

  return (
    <div className="crm-record-table-wrap">
      <table className="crm-record-table" data-density="compact">
        <thead>
          <tr>
            {visibleColumns.map((name) => (
              <th key={name}>{fieldLabel(schema, name)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {result.records.map((row) => (
            <tr key={row.id}>
              {visibleColumns.map((name, idx) => {
                const { kind, options, writable } = fieldSchemaFor(name)
                const isEditing = editingCell?.id === row.id && editingCell?.field === name
                const value = cellValue(row, name)
                return (
                  <td key={name}>
                    {idx === 0 ? (
                      <button className="crm-record-link" onClick={() => onOpenRecord(String(row.id))}>
                        {formatValue(kind, value) || '(untitled)'}
                      </button>
                    ) : isEditing ? (
                      <FieldInput
                        kind={kind}
                        value={value}
                        options={options}
                        autoFocus
                        onChange={(v) => saveCell(row, name, v)}
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
                        {formatValue(kind, value)}
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

function fieldLabel(schema: EntitySchema, name: string): string {
  if (schema.fields[name]) return name.replace(/_/g, ' ')
  const custom = schema.custom_fields.find((c) => c.key === name)
  return custom?.label || name
}

export async function createRecord(entity: string, values: Record<string, unknown>): Promise<RecordRow> {
  const res = await apiPost<{ record: RecordRow }>(`/records/${entity}`, values)
  return res.record
}
