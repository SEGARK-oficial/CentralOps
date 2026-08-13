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
  // Fallback = nome do PRODUTO, não texto de rascunho. O padrão anterior era o
  // literal "Sua Empresa", que aparecia no header e na tela de login em toda
  // instalação sem marca configurada, e também no primeiro paint de qualquer sessão
  // (antes de /auth/status responder). Quem faz white-label sobrescreve pelo campo.
  const [companyName, setCompanyName] = useState("CentralOps")
  const [companyPortalName, setCompanyPortalName] = useState("Portal de Login")
  const [ssoEnabled, setSsoEnabled] = useState(false)
  const [ssoButtonLabel, setSsoButtonLabel] = useState("Entrar com Microsoft")

  const refreshSession = async () => {
    setLoading(true)

    try {
      const status = await api.getAuthStatus()
      setSetupRequired(status.setup_required)
      setCompanyName(status.company_name || "CentralOps")
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
      setCompanyName("CentralOps")
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
    // `PlatformContext` guarda a org/plataforma/integração selecionada em
    // localStorage para sobreviver a um F5. Sem limpar aqui, a seleção
    // sobrevive TAMBÉM à troca de usuário: o próximo login nesta aba (outro
    // operador, outra sessão) herda a org de quem saiu, e o dashboard nega
    // acesso à própria org de quem entrou — porque o filtro enviado é de
    // outra pessoa. `PlatformContext` também reconcilia isso ao carregar as
    // orgs do usuário atual; isto aqui é a defesa na origem.
    localStorage.removeItem("centralops_org_id")
    localStorage.removeItem("centralops_platform")
    localStorage.removeItem("centralops_integration_id")
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
