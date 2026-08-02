import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { LoginPage } from '../auth/LoginPage'
import { SetupPage } from '../auth/SetupPage'
import { Shell } from './Shell'
import { EntityListPage } from '../records/EntityListPage'
import { useEntityList } from '../records/useEntitySchema'
import { LlmSettingsPage } from '../settings/LlmSettingsPage'
import { ConnectedAccountsPage } from '../settings/ConnectedAccountsPage'

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
        <Route index element={<HomeRedirect />} />
        <Route path="e/:entity" element={<EntityListPage />} />
        <Route path="e/:entity/:recordId" element={<EntityListPage />} />
        {/* Not "/settings/*" -- that prefix is proxied straight to the
            backend API (vite.config.ts), so a page reload here would hit
            GET /settings/llm on the server instead of the SPA shell. */}
        <Route path="admin/llm-settings" element={<LlmSettingsPage />} />
        <Route path="admin/connected-accounts" element={<ConnectedAccountsPage />} />
        <Route path="*" element={<HomeRedirect />} />
      </Route>
    </Routes>
  )
}

/** Lands on the first entity the user can read -- there is no hardcoded
 * "home" entity, since which entities exist is itself driven by the
 * registry (R4). */
function HomeRedirect() {
  const { entities, loading } = useEntityList()
  if (loading) return <div className="crm-app-loading">Loading…</div>
  if (entities.length === 0) return <div className="crm-table-status">No entities available.</div>
  return <Navigate to={`/e/${entities[0].name}`} replace />
}
