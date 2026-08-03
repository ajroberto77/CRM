import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { useEntityList } from '../records/useEntitySchema'
import { CommandPalette } from '../command/CommandPalette'
import { navLinkClass } from '../lib/navLinkClass'

const sidebarLinkClass = navLinkClass('crm-sidebar-link')
const settingsLinkClass = navLinkClass('crm-titlebar-settings')

// Settings owns the whole content area below the titlebar, with its own
// section nav (SettingsShell.tsx) -- mirroring CATO's own settings panel,
// which replaces its tab-bar+body entirely rather than opening beside it.
// The entity sidebar is record/list navigation; showing it next to
// Settings' own section nav put two unrelated navs on screen at once.
const SETTINGS_PATH_PREFIX = '/admin/settings'

export function Shell() {
  const { user, org, logout } = useAuth()
  const { entities } = useEntityList()
  const location = useLocation()
  const inSettings = location.pathname.startsWith(SETTINGS_PATH_PREFIX)

  return (
    <div className="crm-shell">
      <CommandPalette />
      <div className="crm-titlebar">
        <div className="crm-titlebar-logo">Greens Ledge</div>
        {org?.name && (
          <div className="crm-titlebar-org">
            <span className="crm-titlebar-org-sep" aria-hidden="true">
              /
            </span>
            {org.name}
          </div>
        )}
        <div className="crm-titlebar-spacer" />
        <NavLink to={SETTINGS_PATH_PREFIX} className={settingsLinkClass} aria-label="Settings" title="Settings">
          <span aria-hidden="true">⚙</span>
        </NavLink>
        <div className="crm-titlebar-user">{user?.name || user?.email}</div>
        <button className="crm-titlebar-logout" onClick={() => logout()}>
          Sign out
        </button>
      </div>
      <div className="crm-shell-body">
        {!inSettings && (
          <nav className="crm-sidebar">
            <NavLink to="/" end className={sidebarLinkClass}>
              Home
            </NavLink>
            <NavLink to="/review/proposals" className={sidebarLinkClass}>
              Pending Proposals
            </NavLink>
            <div className="crm-sidebar-divider" />
            {entities
              // proposed_change already has its own dedicated "Pending
              // Proposals" entry above (server/core/proposals.py's approval
              // queue, with a real approve/decline UI) -- the generic
              // registry-driven list view is not that flow, so don't show
              // both.
              .filter((e) => e.name !== 'proposed_change')
              .map((e) => (
                <NavLink key={e.name} to={`/e/${e.name}`} className={sidebarLinkClass}>
                  {e.label}
                </NavLink>
              ))}
          </nav>
        )}
        <main className="crm-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
