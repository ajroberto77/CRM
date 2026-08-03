import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { useEntityList } from '../records/useEntitySchema'
import { CommandPalette } from '../command/CommandPalette'

const sidebarLinkClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? 'crm-sidebar-link crm-sidebar-link-active' : 'crm-sidebar-link'

export function Shell() {
  const { user, org, logout } = useAuth()
  const { entities } = useEntityList()

  return (
    <div className="crm-shell">
      <CommandPalette />
      <div className="crm-titlebar">
        <div className="crm-titlebar-logo">CRM</div>
        {org?.name && <div className="crm-titlebar-org">{org.name}</div>}
        <div className="crm-titlebar-spacer" />
        <div className="crm-titlebar-user">{user?.name || user?.email}</div>
        <button className="crm-titlebar-logout" onClick={() => logout()}>
          Sign out
        </button>
      </div>
      <div className="crm-shell-body">
        <nav className="crm-sidebar">
          <NavLink to="/" end className={sidebarLinkClass}>
            Home
          </NavLink>
          <NavLink to="/review/proposals" className={sidebarLinkClass}>
            Pending Proposals
          </NavLink>
          <NavLink to="/admin/settings" className={sidebarLinkClass}>
            <span className="crm-sidebar-settings-icon" aria-hidden="true">
              ⚙
            </span>
            Settings
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
        <main className="crm-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
