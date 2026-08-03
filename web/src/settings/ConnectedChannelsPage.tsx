import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { apiDelete, apiGet, apiPost, ApiError } from '../lib/api'

/**
 * M6's connected-channels page -- link a Signal or Telegram identity to
 * your own account, so a swipe-reply "yes"/"no" to a proposal
 * notification (server/core/scheduling_pipeline.py's
 * `_notify_pending()`) resolves back to you
 * (server/core/user_channels.py). Every user manages their own linked
 * channels, same "not admin-gated, this is a personal action" shape as
 * ConnectedAccountsPage.
 *
 * Unlike Connected Accounts, linking here is a plain form submit, not a
 * redirect -- there is no OAuth consent screen for a phone number or a
 * Telegram handle.
 */

const KINDS = ['signal', 'telegram'] as const
type Kind = (typeof KINDS)[number]

const KIND_LABEL: Record<Kind, string> = {
  signal: 'Signal',
  telegram: 'Telegram',
}

const VALUE_PLACEHOLDER: Record<Kind, string> = {
  signal: '+15550100123',
  telegram: '@yourhandle',
}

interface ConnectedChannel {
  id: string
  kind: Kind
  value_raw: string
  created_at: string
}

export function ConnectedChannelsPage() {
  const [channels, setChannels] = useState<ConnectedChannel[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kind, setKind] = useState<Kind>('signal')
  const [value, setValue] = useState('')
  const [linking, setLinking] = useState(false)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await apiGet<{ channels: ConnectedChannel[] }>('/channels')
      setChannels(resp.channels)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to load connected channels')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function link(event: FormEvent) {
    event.preventDefault()
    if (!value.trim()) return
    setLinking(true)
    setError(null)
    try {
      const channel = await apiPost<ConnectedChannel>('/channels/link', { kind, value })
      setChannels((prev) => [...prev, channel])
      setValue('')
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to link that channel')
    } finally {
      setLinking(false)
    }
  }

  async function disconnect(id: string) {
    setDisconnecting(id)
    setError(null)
    try {
      await apiDelete(`/channels/${id}`)
      setChannels((prev) => prev.filter((c) => c.id !== id))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to disconnect')
    } finally {
      setDisconnecting(null)
    }
  }

  if (loading) return <div className="crm-table-status">Loading…</div>

  return (
    <div className="crm-settings-page">
      <div className="crm-entity-page-header">
        <h1>Connected Channels</h1>
      </div>
      <p className="crm-settings-hint">
        Link a Signal number or Telegram handle so you can reply "yes"/"no" to a
        proposal notification right from that app.
      </p>

      {error && <div className="crm-auth-error">{error}</div>}

      <div className="crm-settings-provider-grid">
        {channels.map((channel) => (
          <div key={channel.id} className="crm-settings-provider-card">
            <div className="crm-settings-provider-header">
              <span className="crm-status-dot crm-status-dot-ok" title="linked" />
              <strong>{KIND_LABEL[channel.kind]}</strong>
            </div>
            <div className="crm-settings-hint">{channel.value_raw}</div>
            <div className="crm-settings-row">
              <button
                onClick={() => disconnect(channel.id)}
                disabled={disconnecting === channel.id}
              >
                {disconnecting === channel.id ? 'Disconnecting…' : 'Disconnect'}
              </button>
            </div>
          </div>
        ))}

        <form className="crm-settings-provider-card" onSubmit={link}>
          <div className="crm-settings-provider-header">
            <span className="crm-status-dot crm-status-dot-muted" title="not connected" />
            <strong>Link a channel</strong>
          </div>
          <div className="crm-settings-row">
            <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABEL[k]}
                </option>
              ))}
            </select>
            <input
              type="text"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={VALUE_PLACEHOLDER[kind]}
            />
          </div>
          <div className="crm-settings-row">
            <button className="crm-primary" type="submit" disabled={linking || !value.trim()}>
              {linking ? 'Linking…' : `Connect ${KIND_LABEL[kind]}`}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
