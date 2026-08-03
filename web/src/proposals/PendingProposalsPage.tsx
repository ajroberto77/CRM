import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiPost, withQuery, ApiError } from '../lib/api'
import type { ListResult, RecordRow } from '../records/types'

/**
 * M5's approval queue review page. `proposed_change` is a registered
 * entity (R4) -- the generic /records list endpoint is reused as-is for
 * fetching; this page exists only to add the two real actions (approve/
 * decline) that the generic REST path deliberately blocks (every field on
 * `proposed_change` is writable=False there -- see server/api/proposals.py's
 * docstring). Not folded into the generic RecordDetail component: doing so
 * would mean that shared component branching on one specific entity name,
 * the same class of violation R6 warns against for vertical vocabulary.
 */

interface Payload {
  title?: string
  date?: string
  time?: string
  duration_minutes?: number
  location?: string
  note?: string
}

export function PendingProposalsPage() {
  const [proposalsList, setProposalsList] = useState<RecordRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [acting, setActing] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const path = withQuery('/records/proposed_change', {
        filter: { field: 'status', op: 'eq', value: 'pending' },
        sort: [{ field: 'created_at', direction: 'desc' }],
        limit: 100,
      })
      const resp = await apiGet<ListResult>(path)
      setProposalsList(resp.records)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to load proposals')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function act(id: string, action: 'approve' | 'decline') {
    setActing(id)
    setError(null)
    try {
      await apiPost(`/proposals/${id}/${action}`)
      setProposalsList((prev) => prev.filter((p) => p.id !== id))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : `Failed to ${action}`)
      await load() // someone else may have already decided it -- refresh to show the real state
    } finally {
      setActing(null)
    }
  }

  if (loading) return <div className="crm-table-status">Loading…</div>

  return (
    <div className="crm-settings-page">
      <div className="crm-entity-page-header">
        <h1>Pending Proposals</h1>
      </div>
      <p className="crm-settings-hint">
        Scheduling proposals extracted from your email that haven't auto-approved.
      </p>

      {error && <div className="crm-auth-error">{error}</div>}

      {proposalsList.length === 0 && (
        <div className="crm-table-status">Nothing pending.</div>
      )}

      <div className="crm-settings-provider-grid">
        {proposalsList.map((proposal) => {
          const payload = (proposal.payload as Payload) ?? {}
          return (
            <div key={proposal.id} className="crm-settings-provider-card">
              <div className="crm-settings-provider-header">
                <strong>{payload.title || '(untitled)'}</strong>
              </div>
              <div className="crm-settings-hint">
                {payload.date} {payload.time ? `at ${payload.time}` : '(all day)'}
                {payload.duration_minutes ? ` · ${payload.duration_minutes} min` : ''}
                {payload.location ? ` · ${payload.location}` : ''}
              </div>
              {proposal.category ? (
                <div className="crm-settings-hint">Category: {String(proposal.category)}</div>
              ) : null}
              {typeof proposal.confidence === 'number' && (
                <div className="crm-settings-hint">
                  Confidence: {(proposal.confidence * 100).toFixed(0)}%
                </div>
              )}
              <div className="crm-settings-row">
                <button
                  className="crm-primary"
                  onClick={() => act(String(proposal.id), 'approve')}
                  disabled={acting === proposal.id}
                >
                  {acting === proposal.id ? 'Working…' : 'Approve'}
                </button>
                <button
                  onClick={() => act(String(proposal.id), 'decline')}
                  disabled={acting === proposal.id}
                >
                  Decline
                </button>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
