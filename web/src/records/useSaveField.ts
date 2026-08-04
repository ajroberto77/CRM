import { useState } from 'react'
import { apiPatch, ApiError } from '../lib/api'
import type { EntitySchema, FieldKind, RecordRow } from './types'

interface UseSaveFieldOptions {
  entity: string
  schema: EntitySchema
  /** The patched record, folded back into the caller's own state -- a
   * table's row list, or a single detail record. */
  onSaved: (recordId: string, updated: RecordRow) => void
  /** Runs after every attempt, success or failure -- e.g. closing the
   * inline editor via `shouldAutoCloseOnCommit(kind)`. */
  onSettled?: (recordId: string, field: string, kind: FieldKind) => void
}

/** The one "PATCH a single field on a single record" path (R1) --
 * `RecordTable`'s cell editor, `RecordDetail`'s field editor, and
 * `RecordBoard`'s drag-to-move-lane were three structurally identical calls
 * to `apiPatch`, differing only in how the resulting record folds back into
 * each component's own state (`onSaved`) and whether the caller needs to
 * know success/failure to undo its own optimistic update (`RecordBoard`
 * moves a card immediately, before the request resolves, and rolls back on
 * failure -- the returned boolean is what lets it decide to). */
export function useSaveField({ entity, schema, onSaved, onSettled }: UseSaveFieldOptions) {
  const [saving, setSaving] = useState(false)

  async function saveField(
    recordId: string, name: string, value: unknown, kind: FieldKind,
  ): Promise<boolean> {
    setSaving(true)
    try {
      const isCustom = schema.fields[name] === undefined
      const changes = isCustom ? { custom: { [name]: value } } : { [name]: value }
      const updated = await apiPatch<{ record: RecordRow }>(`/records/${entity}/${recordId}`, { changes })
      onSaved(recordId, updated.record)
      return true
    } catch (err) {
      window.alert(err instanceof ApiError ? err.detail : 'Failed to save')
      return false
    } finally {
      setSaving(false)
      onSettled?.(recordId, name, kind)
    }
  }

  return { saveField, saving }
}
