import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'

// Reuses .crm-sidebar-link/-active -- same nav-link recipe as the main
// sidebar, no reason to fork a second copy of the same rule set.
const subNavLinkClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? 'crm-sidebar-link crm-sidebar-link-active' : 'crm-sidebar-link'

/** The single Settings entry point (Shell.tsx's sidebar) opens this --
 * one full page taking over the content area, with its own left sub-nav,
 * rather than three flat sidebar links indistinguishable from entity
 * records. Every admin-configuration screen lives under here. */
export function SettingsShell() {
  const { user } = useAuth()

  return (
    <div className="crm-settings-shell">
      <nav className="crm-settings-shell-nav">
        {user?.is_admin && (
          <NavLink to="/admin/settings/llm" className={subNavLinkClass}>
            LLM
          </NavLink>
        )}
        <NavLink to="/admin/settings/connected-accounts" className={subNavLinkClass}>
          Connected Accounts
        </NavLink>
        <NavLink to="/admin/settings/connected-channels" className={subNavLinkClass}>
          Connected Channels
        </NavLink>
      </nav>
      <div className="crm-settings-shell-content">
        <Outlet />
      </div>
    </div>
  )
}
