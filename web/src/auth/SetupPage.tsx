import { useState } from 'react'
import type { FormEvent } from 'react'
import { useAuth } from './AuthContext'
import { ApiError } from '../lib/api'

export function SetupPage() {
  const { setup } = useAuth()
  const [orgName, setOrgName] = useState('')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await setup(orgName, email, name, password)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="crm-auth-screen">
      <form className="crm-auth-card" onSubmit={onSubmit}>
        <div className="crm-auth-logo">Greens Ledge</div>
        <h1>Create your organization</h1>
        <p className="crm-auth-subtitle">This is the first-run setup -- it creates the org and its first administrator.</p>
        {error && <div className="crm-auth-error">{error}</div>}
        <label>
          Organization name
          <input value={orgName} onChange={(e) => setOrgName(e.target.value)} required />
        </label>
        <label>
          Your name
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          Email
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="new-password"
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? 'Creating…' : 'Create organization'}
        </button>
      </form>
    </div>
  )
}
