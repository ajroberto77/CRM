import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { LoginPage } from '../auth/LoginPage'
import { SetupPage } from '../auth/SetupPage'
import { Shell } from './Shell'
import { HomePage } from './HomePage'
import { VerticalDashboard } from './VerticalDashboard'
import { EntityListPage } from '../records/EntityListPage'
import { RecordPage } from '../records/RecordPage'
import { SettingsShell } from '../settings/SettingsShell'
import { SettingsEntityListPage } from '../settings/SettingsEntityListPage'
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
        {/* The dedicated full-page record view (Phase 5) -- three regions
            (fields+hierarchy, children, related) instead of the split-panel
            table+detail "/e/..." gives a record following a reference link.
            Not a vite-proxied prefix, so a hard reload/deep link is safe. */}
        <Route path="r/:entity/:recordId" element={<RecordPage />} />
        {/* Phase 12: one dashboard route per registered `nav_group`
            (registry.register_dashboard_tile()), not one route per
            vertical -- a module registering tiles for a new nav_group gets
            a working dashboard here with no change to this file. */}
        <Route path="dashboard/:navGroup" element={<VerticalDashboard />} />
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
          {/* Dynamic entity settings pages -- entities have kebab-case routes but
              snake_case names (e.g., /trusted-sender routes to trusted_sender entity).
              SettingsEntityListPage passes through to EntityListPage, which normalizes. */}
          <Route path=":entity" element={<SettingsEntityListPage />} />
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
