import { useCallback, useEffect, useMemo, useState } from 'react'
import { apiDelete, apiGet, ApiError } from '../lib/api'
import { shouldAutoCloseOnCommit } from './FieldInput'
import { useEntityRoles } from './useEntitySchema'
import { useRecordLabels } from './useRecordLabels'
import { useSaveField } from './useSaveField'
import type { AssociationRoleOption, EntitySchema, RecordRow, RelatedBlocks } from './types'

/** Loads one record plus everything its detail view needs -- the related
 * panel's data, the hierarchical roles that drive `HierarchyChain`, and the
 * field-save/delete mutations -- so `RecordDetail.tsx` (the split view) and
 * `RecordPage.tsx` (the full `/r/:entity/:id` page, Phase 5) share the exact
 * same loading and mutation logic instead of each re-implementing it (R1).
 * Layout is each caller's own concern; this hook owns no JSX. */
export function useRecordDetail(entity: string, recordId: string, schema: EntitySchema) {
  const [record, setRecord] = useState<RecordRow | null>(null)
  const [related, setRelated] = useState<RelatedBlocks>({})
  const [error, setError] = useState<string | null>(null)
  const [editingField, setEditingField] = useState<string | null>(null)
  const { roles, loading: rolesLoading } = useEntityRoles(entity)

  // Every hierarchical role this entity can be the CHILD side of (direction
  // "from") -- e.g. organization is the "from" side of both core's
  // `owned_by` and modules/funds' `rolls_up_to`. Driven entirely by the
  // registry (R6): core never names either role, it just renders whichever
  // hierarchical roles the schema says apply.
  const hierarchicalRoles: AssociationRoleOption[] = useMemo(
    () => roles.filter((r) => r.hierarchical && r.direction === 'from'),
    [roles],
  )

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

  const { saveField, saving: savingField } = useSaveField({
    entity,
    schema,
    onSaved: (_, updated) => setRecord(updated),
    onSettled: (_, __, kind) => {
      if (shouldAutoCloseOnCommit(kind)) setEditingField(null)
    },
  })

  // Confirm-then-delete-then-alert-on-failure was identical in both
  // RecordDetail.tsx and RecordPage.tsx (Phase 5's ui-reviewer pass caught
  // it as a byte-for-byte duplicate) -- centralized here alongside every
  // other mutation this hook already owns, so a caller just wires it to a
  // Delete button's onClick.
  const confirmAndDelete = useCallback(
    async (onDeleted: () => void) => {
      if (!window.confirm(`Delete this ${schema.label.toLowerCase()} record? This cannot be undone.`)) return
      try {
        await apiDelete(`/records/${entity}/${recordId}`)
        onDeleted()
      } catch (err) {
        window.alert(err instanceof ApiError ? err.detail : 'Failed to delete')
      }
    },
    [entity, recordId, schema.label],
  )

  const referenceRefs = useMemo(() => {
    if (!record) return []
    return Object.entries(schema.fields).flatMap(([name, field]) =>
      field.references ? [{ entity: field.references, id: record[name] as string | null }] : [],
    )
  }, [record, schema])
  const { labelFor } = useRecordLabels(referenceRefs)

  // Waits on roles too, not just record+related -- otherwise the hierarchy
  // strip (gated on `hierarchicalRoles`, which depends on `roles`) pops in a
  // beat after the rest of the page has already painted, pushing the layout
  // down. One settled render instead of a visible two-stage layout jump.
  const loading = !record && !error
  const ready = Boolean(record) && !rolesLoading

  return {
    record, related, error, loading, ready,
    hierarchicalRoles, loadRelated,
    saveField, savingField, editingField, setEditingField,
    labelFor, confirmAndDelete,
  }
}
