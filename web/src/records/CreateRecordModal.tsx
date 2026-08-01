import { useState } from 'react'
import { FieldInput } from './FieldInput'
import { createRecord } from './RecordTable'
import { ApiError } from '../lib/api'
import type { EntitySchema, RecordRow } from './types'

interface CreateRecordModalProps {
  entity: string
  schema: EntitySchema
  onCreated: (record: RecordRow) => void
  onClose: () => void
}

export function CreateRecordModal({ entity, schema, onCreated, onClose }: CreateRecordModalProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [custom, setCustom] = useState<Record<string, unknown>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const writableFields = Object.entries(schema.fields).filter(
    ([name, f]) => f.writable && !['owner_id'].includes(name),
  )

  async function onSubmit() {
    setBusy(true)
    setError(null)
    try {
      const body: Record<string, unknown> = { ...values }
      if (Object.keys(custom).length > 0) body.custom = custom
      const record = await createRecord(entity, body)
      onCreated(record)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to create')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="crm-modal-overlay" onClick={onClose}>
      <div className="crm-modal-card" onClick={(e) => e.stopPropagation()}>
        <h2>New {schema.label.replace(/s$/, '')}</h2>
        {error && <div className="crm-auth-error">{error}</div>}
        <div className="crm-modal-fields">
          {writableFields.map(([name, field]) => (
            <label key={name} className="crm-modal-field">
              {name.replace(/_/g, ' ')}
              {field.required && <span className="crm-required-mark"> *</span>}
              <FieldInput
                kind={field.kind}
                value={values[name]}
                options={field.options}
                onChange={(v) => setValues((prev) => ({ ...prev, [name]: v }))}
              />
            </label>
          ))}
          {schema.supports_custom_fields &&
            schema.custom_fields
              .filter((cf) => cf.writable)
              .map((cf) => (
                <label key={cf.key} className="crm-modal-field">
                  {cf.label || cf.key}
                  <FieldInput
                    kind={cf.kind}
                    value={custom[cf.key]}
                    options={cf.options}
                    onChange={(v) => setCustom((prev) => ({ ...prev, [cf.key]: v }))}
                  />
                </label>
              ))}
        </div>
        <div className="crm-modal-actions">
          <button onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button className="crm-primary" onClick={onSubmit} disabled={busy}>
            {busy ? 'Creating…' : 'Create'}
          </button>
        </div>
      </div>
    </div>
  )
}
