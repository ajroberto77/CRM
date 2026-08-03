import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { apiGet, apiPost, ApiError } from '../lib/api'

export interface CurrentUser {
  id: string
  org_id: string
  email: string
  name: string
  is_admin: boolean
  status: string
  team_id: string | null
}

interface ObjectPermissions {
  can_create: boolean
  read_level: string
  edit_level: string
  delete_level: string
  field_masks: string[]
}

interface CurrentOrg {
  id: string
  name: string
}

interface MeResponse {
  user: CurrentUser
  org: CurrentOrg
  is_admin: boolean
  permissions: Record<string, ObjectPermissions>
}

interface AuthState {
  status: 'loading' | 'anonymous' | 'authenticated'
  user: CurrentUser | null
  org: CurrentOrg | null
  permissions: Record<string, ObjectPermissions>
  firstRunRequired: boolean
  login: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
  setup: (orgName: string, email: string, name: string, password: string) => Promise<void>
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthState['status']>('loading')
  const [user, setUser] = useState<CurrentUser | null>(null)
  const [org, setOrg] = useState<CurrentOrg | null>(null)
  const [permissions, setPermissions] = useState<Record<string, ObjectPermissions>>({})
  const [firstRunRequired, setFirstRunRequired] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const setupStatus = await apiGet<{ first_run_required: boolean }>('/setup')
      setFirstRunRequired(setupStatus.first_run_required)
      if (setupStatus.first_run_required) {
        setStatus('anonymous')
        setUser(null)
        setOrg(null)
        return
      }
      const me = await apiGet<MeResponse>('/auth/me')
      setUser(me.user)
      setOrg(me.org)
      setPermissions(me.permissions)
      setStatus('authenticated')
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setStatus('anonymous')
        setUser(null)
        setOrg(null)
        return
      }
      throw err
    }
  }, [])

  useEffect(() => {
    refresh().catch(() => setStatus('anonymous'))
  }, [refresh])

  const login = useCallback(async (email: string, password: string) => {
    await apiPost('/auth/login', { email, password })
    await refresh()
  }, [refresh])

  const logout = useCallback(async () => {
    await apiPost('/auth/logout')
    setUser(null)
    setOrg(null)
    setPermissions({})
    setStatus('anonymous')
  }, [])

  const setup = useCallback(
    async (orgName: string, email: string, name: string, password: string) => {
      await apiPost('/setup', { org_name: orgName, email, name, password })
      await refresh()
    },
    [refresh],
  )

  const value = useMemo<AuthState>(
    () => ({ status, user, org, permissions, firstRunRequired, login, logout, setup, refresh }),
    [status, user, org, permissions, firstRunRequired, login, logout, setup, refresh],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
