"use client"

import type React from "react"
import { createContext, useContext, useEffect, useState } from "react"
import i18n from "@/i18n"
import * as api from "@/services/api"

/** Apply the user's saved language preference (highest-priority
 *  source, above the browser default) when the session is (re)established. */
function applyUserLocale(user: { locale?: string | null } | null): void {
  if (user?.locale && user.locale !== i18n.language) void i18n.changeLanguage(user.locale)
}
import type {
  AuthUser,
  BootstrapAdminRequest,
  LoginRequest,
} from "@/types"

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  setupRequired: boolean
  companyName: string
  companyPortalName: string
  ssoEnabled: boolean
  ssoButtonLabel: string
  login: (credentials: LoginRequest) => Promise<void>
  bootstrapAdmin: (payload: BootstrapAdminRequest) => Promise<void>
  logout: () => Promise<void>
  refreshSession: () => Promise<void>
  /** Mescla campos no usuário atual SEM recarregar a sessão (evita o flash da
   *  tela de loading). Usado pela página de conta ao salvar o próprio perfil,
   *  para que o header/menu reflitam nome/idioma na hora. */
  updateUser: (partial: Partial<AuthUser>) => void
  /** Verifica permissão diretamente pelo contexto (alternativa ao hook usePermission) */
  hasPermission: (perm: string) => boolean
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

interface AuthProviderProps {
  children: React.ReactNode
}

export const AuthProvider: React.FC<AuthProviderProps> = ({ children }) => {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)
  const [setupRequired, setSetupRequired] = useState(false)
  const [companyName, setCompanyName] = useState("Sua Empresa")
  const [companyPortalName, setCompanyPortalName] = useState("Portal de Login")
  const [ssoEnabled, setSsoEnabled] = useState(false)
  const [ssoButtonLabel, setSsoButtonLabel] = useState("Entrar com Microsoft")

  const refreshSession = async () => {
    setLoading(true)

    try {
      const status = await api.getAuthStatus()
      setSetupRequired(status.setup_required)
      setCompanyName(status.company_name || "Sua Empresa")
      setCompanyPortalName(status.company_portal_name || "Portal de Login")
      setSsoEnabled(Boolean(status.sso_enabled))
      setSsoButtonLabel(status.sso_button_label || "Entrar com Microsoft")

      if (status.setup_required) {
        setUser(null)
        return
      }

      try {
        const currentUser = await api.getCurrentUser()
        setUser(currentUser)
        applyUserLocale(currentUser)
      } catch {
        setUser(null)
      }
    } catch {
      setSetupRequired(false)
      setCompanyName("Sua Empresa")
      setCompanyPortalName("Portal de Login")
      setSsoEnabled(false)
      setSsoButtonLabel("Entrar com Microsoft")
      setUser(null)
    } finally {
      setLoading(false)
    }
  }

  const hasPermission = (perm: string): boolean => {
    if (!user) return false
    return user.permissions.includes(perm)
  }

  const updateUser = (partial: Partial<AuthUser>) => {
    setUser((prev) => (prev ? { ...prev, ...partial } : prev))
    if (partial.locale) applyUserLocale({ locale: partial.locale })
  }

  const login = async (credentials: LoginRequest) => {
    const response = await api.login(credentials)
    setSetupRequired(false)
    setUser(response.user)
    applyUserLocale(response.user)
  }

  const bootstrapAdmin = async (payload: BootstrapAdminRequest) => {
    const response = await api.bootstrapAdmin(payload)
    setSetupRequired(false)
    setUser(response.user)
    applyUserLocale(response.user)
  }

  const logout = async () => {
    try {
      await api.logout()
    } catch {
      // If the session is already gone, we still clear the local state.
    }
    setUser(null)
  }

  useEffect(() => {
    refreshSession()
  }, [])

  useEffect(() => {
    const handleAuthExpired = () => {
      setUser(null)
    }

    window.addEventListener("app-auth-expired", handleAuthExpired)
    return () => {
      window.removeEventListener("app-auth-expired", handleAuthExpired)
    }
  }, [])

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        setupRequired,
        companyName,
        companyPortalName,
        ssoEnabled,
        ssoButtonLabel,
        login,
        bootstrapAdmin,
        logout,
        refreshSession,
        updateUser,
        hasPermission,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)

  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider")
  }

  return context
}
