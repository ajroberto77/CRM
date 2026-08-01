import { useCallback, useEffect, useState } from 'react'
import { apiDelete, apiGet, apiPatch, ApiError } from '../lib/api'
import { formatValue } from '../lib/format'
import { FieldInput } from './FieldInput'
import { LinkRecordControl } from './LinkRecordControl'
import type { EntitySchema, RecordRow, RelatedBlocks } from './types'

/** Related records don't carry the target entity's schema, so this falls
 * back across the common label-ish fields rather than assuming `name`. */
function relatedRecordLabel(record: Record<string, unknown> | undefined): string {
  if (!record) return ''
  const candidate = record.name ?? record.full_name ?? record.title ?? record.filename ?? record.id
  return String(candidate ?? '')
}

interface RecordDetailProps {
  entity: string
  recordId: string
  schema: EntitySchema
  onDeleted: () => void
  onClose: () => void
}

/** Three regions: header (label + destructive actions), the field form, and
 * the related-associations panel -- the layout every entity's detail page
 * uses, driven entirely by the schema (R4). */
export function RecordDetail({ entity, recordId, schema, onDeleted, onClose }: RecordDetailProps) {
  const [record, setRecord] = useState<RecordRow | null>(null)
  const [related, setRelated] = useState<RelatedBlocks>({})
  const [error, setError] = useState<string | null>(null)
  const [editingField, setEditingField] = useState<string | null>(null)

  const loadRelated = useCallback(() => {
    return apiGet<{ related: RelatedBlocks }>(`/records/${entity}/${recordId}/related`).then((res) =>
      setRelated(res.related),
    )
  }, [entity, recordId])

  useEffect(() => {
    let cancelled = false
    setRecord(null)
    setError(null)
    Promise.all([apiGet<{ record: RecordRow }>(`/records/${entity}/${recordId}`), loadRelated()])
      .then(([recordRes]) => {
        if (cancelled) return
        setRecord(recordRes.record)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [entity, recordId, loadRelated])

  async function saveField(name: string, value: unknown) {
    if (!record) return
    const isCustom = schema.fields[name] === undefined
    const changes = isCustom ? { custom: { [name]: value } } : { [name]: value }
    try {
      const updated = await apiPatch<{ record: RecordRow }>(`/records/${entity}/${recordId}`, { changes })
      setRecord(updated.record)
    } catch (err) {
      window.alert(err instanceof ApiError ? err.detail : 'Failed to save')
    } finally {
      setEditingField(null)
    }
  }

  async function handleDelete() {
    if (!window.confirm(`Delete this ${schema.label.toLowerCase()} record? This cannot be undone.`)) return
    try {
      await apiDelete(`/records/${entity}/${recordId}`)
      onDeleted()
    } catch (err) {
      window.alert(err instanceof ApiError ? err.detail : 'Failed to delete')
    }
  }

  if (error) return <div className="crm-table-status crm-table-status-error">{error}</div>
  if (!record) return <div className="crm-table-status">Loading…</div>

  const title = String(record[schema.label_field] ?? record.id)
  const fieldEntries = Object.entries(schema.fields).filter(([name]) => name !== 'id')

  return (
    <div className="crm-record-detail">
      <div className="crm-record-detail-header">
        <button className="crm-detail-close" onClick={onClose} aria-label="Close">
          ×
        </button>
        <h2>{title}</h2>
        <button className="crm-detail-delete" onClick={handleDelete}>
          Delete
        </button>
      </div>

      <div className="crm-record-detail-body">
        <div className="crm-record-detail-fields">
          {fieldEntries.map(([name, field]) => {
            const isEditing = editingField === name
            const value = record[name]
            return (
              <div className="crm-detail-field" key={name}>
                <div className="crm-detail-field-label">{name.replace(/_/g, ' ')}</div>
                {isEditing ? (
                  <FieldInput
                    kind={field.kind}
                    value={value}
                    options={field.options}
                    autoFocus
                    onChange={(v) => saveField(name, v)}
                    onBlur={() => setEditingField(null)}
                  />
                ) : (
                  <div
                    className={field.writable ? 'crm-detail-field-value crm-cell-editable' : 'crm-detail-field-value'}
                    onClick={() => field.writable && setEditingField(name)}
                  >
                    {formatValue(field.kind, value) || <span className="crm-detail-empty">—</span>}
                  </div>
                )}
              </div>
            )
          })}

          {schema.supports_custom_fields &&
            schema.custom_fields.map((cf) => {
              const isEditing = editingField === cf.key
              const value = record.custom?.[cf.key]
              return (
                <div className="crm-detail-field" key={cf.key}>
                  <div className="crm-detail-field-label">{cf.label || cf.key}</div>
                  {isEditing ? (
                    <FieldInput
                      kind={cf.kind}
                      value={value}
                      options={cf.options}
                      autoFocus
                      onChange={(v) => saveField(cf.key, v)}
                      onBlur={() => setEditingField(null)}
                    />
                  ) : (
                    <div
                      className={cf.writable ? 'crm-detail-field-value crm-cell-editable' : 'crm-detail-field-value'}
                      onClick={() => cf.writable && setEditingField(cf.key)}
                    >
                      {formatValue(cf.kind, value) || <span className="crm-detail-empty">—</span>}
                    </div>
                  )}
                </div>
              )
            })}
        </div>

        <div className="crm-record-detail-related">
          <div className="crm-record-detail-related-header">
            <h3>Related</h3>
            <LinkRecordControl entity={entity} recordId={recordId} onLinked={loadRelated} />
          </div>
          {Object.keys(related).length === 0 && <div className="crm-detail-empty">No associations.</div>}
          {Object.entries(related).map(([label, items]) => (
            <div className="crm-related-block" key={label}>
              <div className="crm-related-block-label">{label}</div>
              {items.map((item) => (
                <div className="crm-related-item" key={item.association_id}>
                  {relatedRecordLabel(item.record)}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
