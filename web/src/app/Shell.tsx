import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../auth/AuthContext'
import { useEntityList } from '../records/useEntitySchema'
import { useDashboardNavGroups } from '../records/useDashboardTiles'
import { useSavedViews } from '../views/useSavedViews'
import { CommandPalette } from '../command/CommandPalette'
import { navLinkClass } from '../lib/navLinkClass'
import type { EntitySummary } from '../records/types'

const sidebarLinkClass = navLinkClass('crm-sidebar-link')
const settingsLinkClass = navLinkClass('crm-titlebar-settings')

/** One entity's nav link plus its saved views nested underneath (DESIGN.md's
 * "saved views nested under each object") -- a separate component, not a
 * loop-body hook call, so each entity's `useSavedViews` call is a stable,
 * unconditional hook call for that component instance rather than a
 * rules-of-hooks violation inside `.map()`. */
function EntityNavItem({ entity }: { entity: EntitySummary }) {
  const { views, canUseViews } = useSavedViews(entity.name)
  const location = useLocation()
  const onThisEntity =
    location.pathname === `/e/${entity.name}` || location.pathname.startsWith(`/e/${entity.name}/`)
  const activeViewId = onThisEntity ? new URLSearchParams(location.search).get('view') : null

  return (
    <div className="crm-sidebar-entity-group">
      <NavLink to={`/e/${entity.name}`} className={sidebarLinkClass}>
        {entity.label}
      </NavLink>
      {canUseViews && views.length > 0 && (
        <div className="crm-sidebar-views">
          {views.map((view) => (
            <Link
              key={view.id}
              to={`/e/${entity.name}?view=${view.id}`}
              className={
                view.id === activeViewId
                  ? 'crm-sidebar-view-link crm-sidebar-view-link-active'
                  : 'crm-sidebar-view-link'
              }
            >
              {view.name}
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}

// Settings owns the whole content area below the titlebar, with its own
// section nav (SettingsShell.tsx) -- mirroring CATO's own settings panel,
// which replaces its tab-bar+body entirely rather than opening beside it.
// The entity sidebar is record/list navigation; showing it next to
// Settings' own section nav put two unrelated navs on screen at once.
const SETTINGS_PATH_PREFIX = '/admin/settings'

export function Shell() {
  const { user, org, logout } = useAuth()
  const { entities, error: entitiesError } = useEntityList()
  const { navGroups: dashboardNavGroups } = useDashboardNavGroups()
  const location = useLocation()
  const inSettings = location.pathname.startsWith(SETTINGS_PATH_PREFIX)

  // Filter to primary nav entities and group by nav_group, sorted by nav_order
  const primaryEntities = entities
    .filter((e) => e.nav === 'primary')
    .sort((a, b) => {
      // Sort first by nav_group (empty group first), then by nav_order
      const groupA = a.nav_group || ''
      const groupB = b.nav_group || ''
      if (groupA !== groupB) return groupA.localeCompare(groupB)
      return a.nav_order - b.nav_order
    })

  // Group entities by nav_group
  const groups: Record<string, typeof entities> = {}
  for (const entity of primaryEntities) {
    const group = entity.nav_group || ''
    if (!groups[group]) groups[group] = []
    groups[group].push(entity)
  }

  // Ordered list of unique groups as they appear in the sorted entities
  const groupKeys = Array.from(new Set(primaryEntities.map((e) => e.nav_group || '')))

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
              Scheduling Suggestions
            </NavLink>
            {groupKeys.length > 0 && <div className="crm-sidebar-divider" />}
            {entitiesError && <div className="crm-sidebar-error">Failed to load navigation</div>}
            {groupKeys.map((groupKey) => (
              <div key={groupKey || 'default'}>
                {groupKey && (
                  <div className="crm-sidebar-section-row">
                    <div className="crm-sidebar-section">{groupKey}</div>
                    {dashboardNavGroups.includes(groupKey) && (
                      <Link
                        to={`/dashboard/${groupKey}`}
                        className="crm-sidebar-dashboard-link"
                        title={`${groupKey} dashboard`}
                      >
                        📊
                      </Link>
                    )}
                  </div>
                )}
                {groups[groupKey].map((e) => (
                  <EntityNavItem key={e.name} entity={e} />
                ))}
              </div>
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
