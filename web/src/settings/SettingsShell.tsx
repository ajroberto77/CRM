import { Link, NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { navLinkClass } from '../lib/navLinkClass'

// Reuses .crm-sidebar-link/-active -- same nav-link recipe as the main
// sidebar, no reason to fork a second copy of the same rule set.
const subNavLinkClass = navLinkClass('crm-sidebar-link')

/** The single Settings entry point (Shell.tsx's titlebar gear icon) opens
 * this -- one full page taking over the whole content area (Shell.tsx
 * hides the entity sidebar while this is mounted), with its own section
 * nav and a Close control back to Home. Mirrors CATO's own settings panel
 * (title + Close in a header row, a left section list, a content pane) --
 * a single level of navigation on screen at a time, not a sub-nav sitting
 * beside the still-visible entity sidebar. */
export function SettingsShell() {
  const { user } = useAuth()

  return (
    <div className="crm-settings-shell">
      <div className="crm-settings-shell-header">
        <h1>Settings</h1>
        <Link to="/" className="crm-settings-shell-close">
          ✕ Close
        </Link>
      </div>
      <div className="crm-settings-shell-body">
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
    </div>
  )
}
