import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { LoginPage } from '../auth/LoginPage'
import { SetupPage } from '../auth/SetupPage'
import { Shell } from './Shell'
import { HomePage } from './HomePage'
import { EntityListPage } from '../records/EntityListPage'
import { SettingsShell } from '../settings/SettingsShell'
import { LlmSettingsPage } from '../settings/LlmSettingsPage'
import { ConnectedAccountsPage } from '../settings/ConnectedAccountsPage'
import { ConnectedChannelsPage } from '../settings/ConnectedChannelsPage'
import { PendingProposalsPage } from '../proposals/PendingProposalsPage'

export function App() {
  const { status, firstRunRequired } = useAuth()

  if (status === 'loading') {
    return <div className="crm-app-loading">Loading…</div>
  }

  if (status === 'anonymous') {
    return firstRunRequired ? <SetupPage /> : <LoginPage />
  }

  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<HomePage />} />
        <Route path="e/:entity" element={<EntityListPage />} />
        <Route path="e/:entity/:recordId" element={<EntityListPage />} />
        {/* Not "/settings/*" -- that whole prefix is proxied straight to
            the backend API (vite.config.ts), which really does own
            "/settings/llm" etc. as real JSON routes (server/api/settings.py).
            A hard reload or deep link to a client route living there would
            hit the backend instead of the SPA. Mounted under "admin/" for
            the same reason "review/proposals" (not "/proposals") was
            chosen below -- avoid every proxied API prefix. */}
        <Route path="admin/settings" element={<SettingsShell />}>
          <Route index element={<Navigate to="connected-accounts" replace />} />
          <Route path="llm" element={<LlmSettingsPage />} />
          <Route path="connected-accounts" element={<ConnectedAccountsPage />} />
          <Route path="connected-channels" element={<ConnectedChannelsPage />} />
        </Route>
        {/* Not "/proposals/*" -- that prefix is proxied straight to the
            backend API (vite.config.ts), so a page reload here would hit
            GET /proposals on the server instead of the SPA shell. */}
        <Route path="review/proposals" element={<PendingProposalsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
