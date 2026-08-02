import { useCallback, useEffect, useState } from 'react'
import { apiGet, apiPatch, apiPost, ApiError } from '../lib/api'

/**
 * The LLM axis's settings page (M3) -- the first bespoke page in `web/`.
 * Settings are a per-org singleton keyed by section, not a list of records,
 * so this deliberately does not reuse EntityListPage/RecordDetail (R4
 * doesn't apply here -- see server/api/settings.py's docstring).
 *
 * API keys are NOT edited here. They live in environment variables
 * (server/config.py's `_SECRET_ENV`) and are never stored in the database or
 * a config file -- CLAUDE.md's secrets rule. This page only edits and tests
 * non-secret settings: which provider/model to use, the fallback chain, and
 * Ollama's (non-secret) host/port/Wake-on-LAN fields.
 */

const PROVIDERS = ['ollama', 'openai', 'anthropic', 'gemini', 'claudecode'] as const
type Provider = (typeof PROVIDERS)[number]

const PROVIDER_LABEL: Record<Provider, string> = {
  ollama: 'Ollama (local)',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  gemini: 'Gemini',
  claudecode: 'Claude Code',
}

// Informational only -- which environment variable each keyed provider's
// secret comes from. Mirrors server/config.py's `_SECRET_ENV`; a static list
// here is UI copy, not a second copy of the mapping itself.
const PROVIDER_SECRET_ENV_VAR: Partial<Record<Provider, string>> = {
  openai: 'OPENAI_API_KEY',
  anthropic: 'ANTHROPIC_API_KEY',
  gemini: 'GEMINI_API_KEY',
  claudecode: 'CLAUDECODE_KEY',
}

interface ChainEntry {
  provider: string
  model: string
}

interface LlmSettingsValues {
  provider?: string
  chain?: ChainEntry[]
  ollama_host?: string
  ollama_port?: string
  ollama_mac?: string
  ollama_wol_broadcast?: string
  ollama_wake_timeout?: number
  [key: string]: unknown
}

function modelValue(values: LlmSettingsValues, provider: string): string {
  const raw = values[modelKey(provider)]
  return typeof raw === 'string' ? raw : ''
}

interface ProviderStatus {
  ok: boolean
  error: string | null
}

function modelKey(provider: string): string {
  return `${provider}_model`
}

export function LlmSettingsPage() {
  const [values, setValues] = useState<LlmSettingsValues>({})
  const [status, setStatus] = useState<Record<string, ProviderStatus>>({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saveState, setSaveState] = useState<'idle' | 'saving' | 'saved'>('idle')
  const [testResults, setTestResults] = useState<Record<string, ProviderStatus | 'testing'>>({})

  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const [settingsResp, statusResp] = await Promise.all([
        apiGet<{ values: LlmSettingsValues }>('/settings/llm'),
        apiGet<{ providers: Record<string, ProviderStatus> }>('/settings/llm/status'),
      ])
      setValues(settingsResp.values)
      setStatus(statusResp.providers)
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.detail : 'Failed to load LLM settings')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  function setField<K extends keyof LlmSettingsValues>(key: K, value: LlmSettingsValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }))
    setSaveState('idle')
  }

  function setModel(provider: string, model: string) {
    setValues((prev) => ({ ...prev, [modelKey(provider)]: model }))
    setSaveState('idle')
  }

  async function save() {
    setSaveState('saving')
    setError(null)
    try {
      const resp = await apiPatch<{ values: LlmSettingsValues }>('/settings/llm', { changes: values })
      setValues(resp.values)
      setSaveState('saved')
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Failed to save')
      setSaveState('idle')
    }
  }

  async function refreshStatus() {
    try {
      const resp = await apiGet<{ providers: Record<string, ProviderStatus> }>('/settings/llm/status')
      setStatus(resp.providers)
    } catch {
      // status is best-effort UI decoration -- a failed refresh leaves the
      // last-known dots in place rather than surfacing a page-level error.
    }
  }

  async function testProvider(provider: string, model: string) {
    setTestResults((prev) => ({ ...prev, [provider]: 'testing' }))
    const overrides: Record<string, unknown> = { provider, [modelKey(provider)]: model }
    if (provider === 'ollama') {
      overrides.ollama_host = values.ollama_host
      overrides.ollama_port = values.ollama_port
      overrides.ollama_mac = values.ollama_mac
      overrides.ollama_wol_broadcast = values.ollama_wol_broadcast
      overrides.ollama_wake_timeout = values.ollama_wake_timeout
    }
    try {
      const result = await apiPost<ProviderStatus>('/settings/llm/test', { overrides })
      setTestResults((prev) => ({ ...prev, [provider]: result }))
    } catch (err) {
      setTestResults((prev) => ({
        ...prev,
        [provider]: { ok: false, error: err instanceof ApiError ? err.detail : 'Test failed' },
      }))
    }
  }

  const chain = values.chain ?? []

  function updateChainEntry(index: number, entry: Partial<ChainEntry>) {
    const next = chain.map((c, i) => (i === index ? { ...c, ...entry } : c))
    setField('chain', next)
  }

  function addChainEntry() {
    setField('chain', [...chain, { provider: 'ollama', model: '' }])
  }

  function removeChainEntry(index: number) {
    setField('chain', chain.filter((_, i) => i !== index))
  }

  function moveChainEntry(index: number, delta: number) {
    const target = index + delta
    if (target < 0 || target >= chain.length) return
    const next = [...chain]
    ;[next[index], next[target]] = [next[target], next[index]]
    setField('chain', next)
  }

  if (loading) return <div className="crm-app-loading">Loading…</div>

  if (loadError) {
    return (
      <div className="crm-settings-page">
        <div className="crm-entity-page-header">
          <h1>LLM Provider Settings</h1>
        </div>
        <div className="crm-auth-error">{loadError}</div>
      </div>
    )
  }

  return (
    <div className="crm-settings-page">
      <div className="crm-entity-page-header">
        <h1>LLM Provider Settings</h1>
        <button onClick={refreshStatus}>Refresh status</button>
      </div>

      {error && <div className="crm-auth-error">{error}</div>}

      <section className="crm-settings-section">
        <h2>Active provider</h2>
        <p className="crm-settings-hint">
          Used when no fallback chain is configured below.
        </p>
        <div className="crm-settings-row">
          <select
            aria-label="Active provider"
            value={values.provider ?? 'ollama'}
            onChange={(e) => setField('provider', e.target.value as Provider)}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_LABEL[p]}
              </option>
            ))}
          </select>
          <input
            type="text"
            aria-label="Active provider model"
            placeholder="model (e.g. gpt-4o, claude-opus-4-5)"
            value={modelValue(values, values.provider ?? 'ollama')}
            onChange={(e) => setModel(values.provider ?? 'ollama', e.target.value)}
          />
        </div>
      </section>

      <section className="crm-settings-section">
        <h2>Providers</h2>
        <div className="crm-settings-provider-grid">
          {PROVIDERS.map((provider) => {
            const dot = status[provider]
            const result = testResults[provider]
            const secretVar = PROVIDER_SECRET_ENV_VAR[provider]
            return (
              <div key={provider} className="crm-settings-provider-card">
                <div className="crm-settings-provider-header">
                  <span
                    className={
                      'crm-status-dot ' + (dot?.ok ? 'crm-status-dot-ok' : 'crm-status-dot-error')
                    }
                    title={dot?.error ?? 'reachable'}
                  />
                  <strong>{PROVIDER_LABEL[provider]}</strong>
                </div>
                {secretVar && (
                  <div className="crm-settings-hint">
                    API key comes from the <code>{secretVar}</code> environment variable, not this
                    page.
                  </div>
                )}
                {provider === 'ollama' && (
                  <div className="crm-settings-ollama-fields">
                    <input
                      type="text"
                      aria-label="Ollama host"
                      placeholder="host (default localhost)"
                      value={values.ollama_host ?? ''}
                      onChange={(e) => setField('ollama_host', e.target.value)}
                    />
                    <input
                      type="text"
                      aria-label="Ollama port"
                      placeholder="port (default 11434)"
                      value={values.ollama_port ?? ''}
                      onChange={(e) => setField('ollama_port', e.target.value)}
                    />
                    <input
                      type="text"
                      aria-label="Ollama MAC address"
                      placeholder="MAC address (optional, enables Wake-on-LAN)"
                      value={values.ollama_mac ?? ''}
                      onChange={(e) => setField('ollama_mac', e.target.value)}
                    />
                  </div>
                )}
                <div className="crm-settings-row">
                  <input
                    type="text"
                    aria-label={`${PROVIDER_LABEL[provider]} model to test`}
                    placeholder="model to test"
                    value={modelValue(values, provider)}
                    onChange={(e) => setModel(provider, e.target.value)}
                  />
                  <button
                    onClick={() => testProvider(provider, modelValue(values, provider))}
                    disabled={result === 'testing'}
                  >
                    {result === 'testing' ? 'Testing…' : 'Test connection'}
                  </button>
                </div>
                {result && result !== 'testing' && (
                  <div className={result.ok ? 'crm-settings-test-ok' : 'crm-settings-test-error'}>
                    {result.ok ? 'Connected' : result.error}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </section>

      <section className="crm-settings-section">
        <h2>Fallback chain</h2>
        <p className="crm-settings-hint">
          Tried in order for structured extraction; rolls to the next entry only when a provider is
          unavailable (unreachable, missing key, rate-limited), never on a malformed response. Empty
          falls back to the single active provider above.
        </p>
        <div className="crm-settings-chain">
          {chain.map((entry, index) => (
            <div key={index} className="crm-settings-chain-row">
              <span
                className={
                  'crm-status-dot ' +
                  (status[entry.provider]?.ok ? 'crm-status-dot-ok' : 'crm-status-dot-error')
                }
                title={status[entry.provider]?.error ?? 'reachable'}
              />
              <select
                aria-label={`Step ${index + 1} provider`}
                value={entry.provider}
                onChange={(e) => updateChainEntry(index, { provider: e.target.value })}
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {PROVIDER_LABEL[p]}
                  </option>
                ))}
              </select>
              <input
                type="text"
                aria-label={`Step ${index + 1} model`}
                placeholder="model"
                value={entry.model}
                onChange={(e) => updateChainEntry(index, { model: e.target.value })}
              />
              <button aria-label="Move up" onClick={() => moveChainEntry(index, -1)} disabled={index === 0}>
                ↑
              </button>
              <button
                aria-label="Move down"
                onClick={() => moveChainEntry(index, 1)}
                disabled={index === chain.length - 1}
              >
                ↓
              </button>
              <button onClick={() => removeChainEntry(index)}>Remove</button>
            </div>
          ))}
          <button onClick={addChainEntry}>Add provider to chain</button>
        </div>
      </section>

      <div className="crm-modal-actions">
        <button className="crm-primary" onClick={save} disabled={saveState === 'saving'}>
          {saveState === 'saving' ? 'Saving…' : saveState === 'saved' ? 'Saved' : 'Save'}
        </button>
      </div>
    </div>
  )
}
