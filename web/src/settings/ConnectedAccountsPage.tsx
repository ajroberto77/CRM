import { useCallback, useEffect, useState } from 'react'
import { apiDelete, apiGet, apiPost, ApiError } from '../lib/api'

/**
 * M4's connected-accounts page -- link/disconnect a Microsoft or Google
 * account. Every user manages their own accounts (not admin-gated, unlike
 * LlmSettingsPage) since linking a mailbox/calendar is a personal action.
 *
 * Linking is a real browser redirect, not a fetch: starting a link
 * navigates the whole page to Microsoft/Google's consent screen, which
 * redirects back to the backend's own OAuth callback, which redirects here
 * again with `?linked=1`.
 */

const PROVIDERS = ['microsoft', 'google'] as const
type Provider = (typeof PROVIDERS)[number]

const PROVIDER_LABEL: Record<Provider, string> = {
  microsoft: 'Microsoft 365',
  google: 'Google',
}

// 'active' is the only verified state; 'pending' (link started but never
// completed -- tab closed, consent denied) and 'disabled' are not errors and
// must not read as one, so each status gets its own dot rather than
// collapsing to a plain ok/error boolean the way LlmSettingsPage's provider
// reachability check does.
const STATUS_DOT_CLASS: Record<ConnectedAccount['status'], string> = {
  active: 'crm-status-dot-ok',
  pending: 'crm-status-dot-warn',
  error: 'crm-status-dot-error',
  disabled: 'crm-status-dot-muted',
}

interface ConnectedAccount {
  id: string
  provider: Provider
  email_address: string
  status: 'pending' | 'active' | 'error' | 'disabled'
  status_detail: string
  created_at: string
  updated_at: string
}

export function ConnectedAccountsPage() {
  const [accountsList, setAccountsList] = useState<ConnectedAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [linking, setLinking] = useState<Provider | null>(null)
  const [disconnecting, setDisconnecting] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await apiGet<{ accounts: ConnectedAccount[] }>('/accounts')
      setAccountsList(resp.accounts)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to load connected accounts')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  async function link(provider: Provider) {
    setLinking(provider)
    setError(null)
    try {
      const resp = await apiPost<{ authorize_url: string }>(`/accounts/${provider}/link`)
      window.location.assign(resp.authorize_url)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to start the link flow')
      setLinking(null)
    }
  }

  async function disconnect(id: string) {
    setDisconnecting(id)
    setError(null)
    try {
      await apiDelete(`/accounts/${id}`)
      setAccountsList((prev) => prev.filter((a) => a.id !== id))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to disconnect')
    } finally {
      setDisconnecting(null)
    }
  }

  if (loading) return <div className="crm-table-status">Loading…</div>

  const linkedProviders = new Set(accountsList.map((a) => a.provider))

  return (
    <div className="crm-settings-page">
      <div className="crm-entity-page-header">
        <h1>Connected Accounts</h1>
      </div>
      <p className="crm-settings-hint">
        Link your Microsoft or Google account to sync contacts into Greens Ledge.
      </p>

      {error && <div className="crm-auth-error">{error}</div>}

      <div className="crm-settings-provider-grid">
        {accountsList.map((account) => (
          <div key={account.id} className="crm-settings-provider-card">
            <div className="crm-settings-provider-header">
              <span
                className={'crm-status-dot ' + STATUS_DOT_CLASS[account.status]}
                title={account.status_detail || account.status}
              />
              <strong>{PROVIDER_LABEL[account.provider]}</strong>
            </div>
            <div className="crm-settings-hint">
              {account.email_address || 'Not yet connected'} &mdash; {account.status}
              {account.status_detail && `: ${account.status_detail}`}
            </div>
            <div className="crm-settings-row">
              {account.status !== 'active' && (
                <button
                  className="crm-primary"
                  onClick={() => link(account.provider)}
                  disabled={linking === account.provider || disconnecting === account.id}
                >
                  {linking === account.provider ? 'Redirecting…' : 'Reconnect'}
                </button>
              )}
              <button
                onClick={() => disconnect(account.id)}
                disabled={disconnecting === account.id || linking === account.provider}
              >
                {disconnecting === account.id ? 'Disconnecting…' : 'Disconnect'}
              </button>
            </div>
          </div>
        ))}

        {PROVIDERS.filter((p) => !linkedProviders.has(p)).map((provider) => (
          <div key={provider} className="crm-settings-provider-card">
            <div className="crm-settings-provider-header">
              <span className="crm-status-dot crm-status-dot-muted" title="not connected" />
              <strong>{PROVIDER_LABEL[provider]}</strong>
            </div>
            <div className="crm-settings-row">
              <button
                className="crm-primary"
                onClick={() => link(provider)}
                disabled={linking === provider}
              >
                {linking === provider ? 'Redirecting…' : `Connect ${PROVIDER_LABEL[provider]}`}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
