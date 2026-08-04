import { fieldLabel } from '../lib/format'
import { FieldInput } from './FieldInput'
import { FieldValue } from './FieldValue'
import type { EntitySchema, FieldKind, RecordRow } from './types'

interface RecordFieldListProps {
  schema: EntitySchema
  record: RecordRow
  editingField: string | null
  setEditingField: (field: string | null) => void
  saveField: (recordId: string, field: string, value: unknown, kind: FieldKind) => void
  savingField: boolean
  labelFor: (entity: string, id: string | null) => string | null
}

/** The inline-editable field form -- every entity's fields plus its custom
 * fields, in read mode (`FieldValue`) or edit mode (`FieldInput`) per field.
 * The one renderer (R1) for a record's own fields, shared by `RecordDetail`
 * (the split view) and `RecordPage` (the full `/r/:entity/:id` page, Phase
 * 5) rather than each keeping its own copy of this same field loop. */
export function RecordFieldList({
  schema, record, editingField, setEditingField, saveField, savingField, labelFor,
}: RecordFieldListProps) {
  const fieldEntries = Object.entries(schema.fields).filter(([name]) => name !== 'id')

  return (
    <div className="crm-record-detail-fields">
      {fieldEntries.map(([name, field]) => {
        const isEditing = editingField === name
        const value = record[name]
        return (
          <div className="crm-detail-field" key={name}>
            <div className="crm-detail-field-label">{fieldLabel(field, name)}</div>
            {isEditing ? (
              <FieldInput
                kind={field.kind}
                value={value}
                options={field.options}
                references={field.references}
                autoFocus
                onChange={(v) => saveField(String(record.id), name, v, field.kind)}
                onBlur={() => setEditingField(null)}
              />
            ) : (
              <div
                className={field.writable ? 'crm-detail-field-value crm-cell-editable' : 'crm-detail-field-value'}
                onClick={() => field.writable && !savingField && setEditingField(name)}
              >
                {value === null || value === undefined || value === '' ? (
                  <span className="crm-detail-empty">—</span>
                ) : (
                  <FieldValue
                    kind={field.kind}
                    value={value}
                    currency={record.currency as string | undefined}
                    referenceEntity={field.references}
                    referenceLabel={field.references ? labelFor(field.references, value as string | null) : null}
                  />
                )}
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
                  onChange={(v) => saveField(String(record.id), cf.key, v, cf.kind)}
                  onBlur={() => setEditingField(null)}
                />
              ) : (
                <div
                  className={cf.writable ? 'crm-detail-field-value crm-cell-editable' : 'crm-detail-field-value'}
                  onClick={() => cf.writable && !savingField && setEditingField(cf.key)}
                >
                  {value === null || value === undefined || value === '' ? (
                    <span className="crm-detail-empty">—</span>
                  ) : (
                    <FieldValue kind={cf.kind} value={value} />
                  )}
                </div>
              )}
            </div>
          )
        })}
    </div>
  )
}
