import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiGet, ApiError } from '../lib/api'
import { recordLabel } from '../lib/format'
import type { HierarchyStep } from './types'

interface HierarchyChainProps {
  entity: string
  recordId: string
  role: string
  label: string
}

/** The ancestor chain for one hierarchical association role, e.g. "Owned
 * by: Parent Co › Ultimate Parent Ltd" -- the one generic renderer for
 * `GET /records/{entity}/{id}/hierarchy` (R1). `RecordDetail.tsx`
 * instantiates this once per hierarchical role the entity's own roles list
 * (`GET /records/{entity}/roles`) says applies as the "from" (child) side --
 * core's `owned_by` (Ultimate Parent Company) and `modules/funds`'
 * `rolls_up_to` (investment rollup) render through the exact same component,
 * not two hand-written chains, so a third hierarchical role a future module
 * registers needs no change here either.
 *
 * Renders nothing while loading or if the chain is empty (the record has no
 * parent for this role) -- there is nothing informative to show either way,
 * and an empty "Owned by:" label would just be noise. */
export function HierarchyChain({ entity, recordId, role, label }: HierarchyChainProps) {
  const [chain, setChain] = useState<HierarchyStep[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setChain(null)
    setError(null)
    apiGet<{ chain: HierarchyStep[] }>(
      `/records/${entity}/${recordId}/hierarchy?role=${encodeURIComponent(role)}&direction=ancestors`,
    )
      .then((r) => {
        if (!cancelled) setChain(r.chain)
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.detail : String(err))
      })
    return () => {
      cancelled = true
    }
  }, [entity, recordId, role])

  if (error) return <div className="crm-hierarchy-chain crm-hierarchy-chain-error">{label}: {error}</div>
  if (!chain || chain.length === 0) return null

  return (
    <div className="crm-hierarchy-chain">
      <span className="crm-hierarchy-chain-label">{label}</span>
      {chain.map((step, i) => (
        <span key={`${step.entity_type}:${step.entity_id}`} className="crm-hierarchy-chain-step">
          {i > 0 && <span className="crm-hierarchy-chain-sep">›</span>}
          <Link className="crm-reference-link" to={`/e/${step.entity_type}/${step.entity_id}`}>
            {recordLabel(step.record)}
          </Link>
        </span>
      ))}
    </div>
  )
}
