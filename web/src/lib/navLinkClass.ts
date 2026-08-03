/** The one `<NavLink>` active-class toggler -- `${base}` when inactive,
 * `${base} ${base}-active` when active. Shell.tsx's sidebar links, its
 * titlebar settings icon, and SettingsShell.tsx's sub-nav all want exactly
 * this, differing only in which base class name they pass. */
export function navLinkClass(base: string) {
  return ({ isActive }: { isActive: boolean }) => (isActive ? `${base} ${base}-active` : base)
}
