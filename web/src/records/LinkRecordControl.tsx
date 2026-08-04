import { useEffect, useState } from 'react'
import { apiGet, apiPost, ApiError } from '../lib/api'
import { recordLabel } from '../lib/format'
import { useEntitySchema } from './useEntitySchema'
import { useRecordSearch } from './useRecordSearch'
import type { ListResult, RecordRow } from './types'

interface RoleOption {
  role: string
  direction: 'from' | 'to'
  label: string
  target_types: string[]
}

interface LinkRecordControlProps {
  entity: string
  recordId: string
  onLinked: () => void
}

/** The one role whose relationship carries a further, structured choice --
 * `principal_of`'s functional title at the GP, drawn from the admin-
 * configurable `gp_role` reference list (`modules/funds`). Named here
 * rather than a generic `attributes` editor, matching this file's own
 * "schema-driven, no change for a new role" design for every OTHER role --
 * see RecordBoard.tsx's `deal.stage` exception for the same precedent. */
const GP_ROLE_ASSOCIATION = 'principal_of'

/** The "+ Link" control on a record's detail page -- picks a role (from
 * GET /records/{entity}/roles), a target type, then a target record via
 * live search, and POSTs /associations. Entirely schema-driven: a new role
 * registered by any module needs no change here (R4). */
export function LinkRecordControl({ entity, recordId, onLinked }: LinkRecordControlProps) {
  const [open, setOpen] = useState(false)
  const [roles, setRoles] = useState<RoleOption[]>([])
  const [selectedRole, setSelectedRole] = useState<RoleOption | null>(null)
  const [targetType, setTargetType] = useState<string>('')
  const { schema: targetSchema } = useEntitySchema(targetType || undefined)
  const [query, setQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [gpRoles, setGpRoles] = useState<RecordRow[]>([])
  const [gpRoleKey, setGpRoleKey] = useState('')

  useEffect(() => {
    if (!open) return
    apiGet<{ roles: RoleOption[] }>(`/records/${entity}/roles`).then((r) => setRoles(r.roles))
  }, [open, entity])

  useEffect(() => {
    setGpRoleKey('')
    if (selectedRole?.role !== GP_ROLE_ASSOCIATION) {
      setGpRoles([])
      return
    }
    // gp_role is admin_only (like investor_category) -- a non-admin's fetch
    // 403s. Degrade to no dropdown rather than break the rest of the link
    // flow; the association still works, just without a title, exactly
    // like it did before this field existed. Only the expected 403 is
    // swallowed (same narrowing as useSavedViews.ts's own 403 case) -- any
    // other failure surfaces through this component's existing error state
    // rather than silently looking identical to "not an admin."
    apiGet<ListResult>('/records/gp_role')
      .then((r) => setGpRoles(r.records))
      .catch((err) => {
        if (err instanceof ApiError && err.status === 403) {
          setGpRoles([])
          return
        }
        setError(err instanceof ApiError ? err.detail : 'Failed to load GP roles')
      })
  }, [selectedRole])

  const { results, loading: searching, error: searchError } = useRecordSearch(
    targetType || null, targetSchema, query,
  )

  async function link(target: RecordRow) {
    if (!selectedRole) return
    setError(null)
    try {
      const attributes = gpRoleKey ? { gp_role_key: gpRoleKey } : {}
      const body =
        selectedRole.direction === 'from'
          ? { role: selectedRole.role, from_type: entity, from_id: recordId, to_type: targetType, to_id: String(target.id), attributes }
          : { role: selectedRole.role, from_type: targetType, from_id: String(target.id), to_type: entity, to_id: recordId, attributes }
      await apiPost('/associations', body)
      setOpen(false)
      setSelectedRole(null)
      setTargetType('')
      setQuery('')
      setGpRoleKey('')
      onLinked()
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to link')
    }
  }

  if (!open) {
    return (
      <button className="crm-link-record-toggle" onClick={() => setOpen(true)}>
        + Link
      </button>
    )
  }

  return (
    <div className="crm-link-record-form">
      {(error || searchError) && <div className="crm-auth-error">{error || searchError}</div>}
      <select
        value={selectedRole?.role ?? ''}
        onChange={(e) => {
          const r = roles.find((opt) => `${opt.role}:${opt.direction}` === e.target.value) ?? null
          setSelectedRole(r)
          setTargetType(r && r.target_types.length === 1 ? r.target_types[0] : '')
        }}
      >
        <option value="">Relationship…</option>
        {roles.map((r) => (
          <option key={`${r.role}:${r.direction}`} value={`${r.role}:${r.direction}`}>
            {r.label}
          </option>
        ))}
      </select>

      {selectedRole && selectedRole.target_types.length > 1 && (
        <select value={targetType} onChange={(e) => setTargetType(e.target.value)}>
          <option value="">Record type…</option>
          {selectedRole.target_types.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      )}

      {selectedRole?.role === GP_ROLE_ASSOCIATION && gpRoles.length > 0 && (
        <select value={gpRoleKey} onChange={(e) => setGpRoleKey(e.target.value)}>
          <option value="">Title (optional)…</option>
          {gpRoles.map((r) => (
            <option key={String(r.id)} value={String(r.key)}>
              {String(r.label)}
            </option>
          ))}
        </select>
      )}

      {selectedRole && targetType && (
        <div className="crm-link-record-search">
          <input
            placeholder="Search…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            autoFocus
          />
          {query.trim() && (
            <div className="crm-record-picker-results">
              {searching && <div className="crm-record-picker-status">Searching…</div>}
              {searchError && (
                <div className="crm-record-picker-status crm-record-picker-error">{searchError}</div>
              )}
              {!searching && !searchError && results.length === 0 && (
                <div className="crm-record-picker-status">No matches.</div>
              )}
              {results.map((r) => (
                <button key={String(r.id)} className="crm-record-picker-result" onClick={() => link(r)}>
                  {recordLabel(r, targetSchema?.label_field)}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <button
        className="crm-link-record-cancel"
        onClick={() => {
          setOpen(false)
          setSelectedRole(null)
          setTargetType('')
          setQuery('')
          setGpRoleKey('')
        }}
      >
        Cancel
      </button>
    </div>
  )
}
