import { NavLink, Outlet } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { useEntityList } from '../records/useEntitySchema'

export function Shell() {
  const { user, logout } = useAuth()
  const { entities } = useEntityList()

  return (
    <div className="crm-shell">
      <div className="crm-titlebar">
        <div className="crm-titlebar-logo">CRM</div>
        <div className="crm-titlebar-spacer" />
        <div className="crm-titlebar-user">{user?.name || user?.email}</div>
        <button className="crm-titlebar-logout" onClick={() => logout()}>
          Sign out
        </button>
      </div>
      <div className="crm-shell-body">
        <nav className="crm-sidebar">
          {entities.map((e) => (
            <NavLink key={e.name} to={`/e/${e.name}`} className={({ isActive }) => (isActive ? 'crm-sidebar-link crm-sidebar-link-active' : 'crm-sidebar-link')}>
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
