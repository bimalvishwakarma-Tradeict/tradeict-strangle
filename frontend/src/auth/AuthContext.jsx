import { createContext, useCallback, useContext, useMemo, useState } from 'react'
import {
  clearAuthSession,
  getStoredAuthUser,
  getStoredToken,
  logout as apiLogout,
  storeAuthSession,
} from '../services/api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const stored = getStoredAuthUser()
  const [token, setToken] = useState(() => getStoredToken())
  const [email, setEmail] = useState(() => stored?.email || '')
  const [role, setRole] = useState(() => stored?.role || '')
  const [mustChangePassword, setMustChangePassword] = useState(
    () => Boolean(stored?.must_change_password),
  )

  const setSession = useCallback((session) => {
    const next = {
      token: session.token || '',
      email: session.email || '',
      role: session.role || '',
      must_change_password: Boolean(session.must_change_password),
    }
    storeAuthSession(next)
    setToken(next.token)
    setEmail(next.email)
    setRole(next.role)
    setMustChangePassword(next.must_change_password)
  }, [])

  const logoutLocal = useCallback(() => {
    clearAuthSession()
    setToken('')
    setEmail('')
    setRole('')
    setMustChangePassword(false)
  }, [])

  const logout = useCallback(async () => {
    try {
      if (token) {
        await apiLogout()
      }
    } catch {
      clearAuthSession()
    }
    setToken('')
    setEmail('')
    setRole('')
    setMustChangePassword(false)
  }, [token])

  const value = useMemo(
    () => ({
      token,
      email,
      role,
      mustChangePassword,
      setSession,
      logout,
      logoutLocal,
      isAdmin: role === 'admin',
    }),
    [
      token,
      email,
      role,
      mustChangePassword,
      setSession,
      logout,
      logoutLocal,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return ctx
}
