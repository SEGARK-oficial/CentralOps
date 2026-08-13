/**
 * PlatformContext
 * Global context for organization, platform, and integration selection.
 * Persists selections to localStorage for session continuity.
 */

import type React from "react"
import { createContext, useContext, useCallback, useEffect, useState, useMemo } from "react"
import type { Integration, Organization, PlatformType } from "@/types"
import * as api from "@/services/api"
import { useAuth } from "./AuthContext"

interface PlatformContextValue {
  // Data
  organizations: Organization[]
  integrations: Integration[]
  loading: boolean
  /** Erro ao carregar orgs/integrações — distingue "falha" de "lista vazia". */
  error: string | null

  // Selections
  selectedOrgId: number | null
  selectedPlatform: PlatformType | null
  selectedIntegrationId: number | null

  // Setters
  setSelectedOrgId: (id: number | null) => void
  setSelectedPlatform: (platform: PlatformType | null) => void
  setSelectedIntegrationId: (id: number | null) => void

  // Derived
  selectedOrganization: Organization | null
  selectedIntegration: Integration | null
  filteredIntegrations: Integration[]

  // Refresh
  refreshData: () => Promise<void>
  clearFilters: () => void
}

const PlatformContext = createContext<PlatformContextValue | null>(null)

/** Dono da seleção guardada em localStorage. Ver o efeito de reconciliação. */
const SCOPE_OWNER_KEY = "centralops_scope_owner"

export const PlatformProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { user } = useAuth()
  const userId = user?.id ?? null
  const [organizations, setOrganizations] = useState<Organization[]>([])
  const [integrations, setIntegrations] = useState<Integration[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [selectedOrgId, setSelectedOrgIdState] = useState<number | null>(() => {
    const stored = localStorage.getItem("centralops_org_id")
    return stored ? Number(stored) : null
  })
  const [selectedPlatform, setSelectedPlatformState] = useState<PlatformType | null>(() => {
    return (localStorage.getItem("centralops_platform") as PlatformType) || null
  })
  const [selectedIntegrationId, setSelectedIntegrationIdState] = useState<number | null>(() => {
    const stored = localStorage.getItem("centralops_integration_id")
    return stored ? Number(stored) : null
  })

  const setSelectedOrgId = useCallback((id: number | null) => {
    setSelectedOrgIdState(id)
    if (id) localStorage.setItem("centralops_org_id", String(id))
    else localStorage.removeItem("centralops_org_id")
  }, [])

  const setSelectedPlatform = useCallback((platform: PlatformType | null) => {
    setSelectedPlatformState(platform)
    if (platform) localStorage.setItem("centralops_platform", platform)
    else localStorage.removeItem("centralops_platform")
  }, [])

  const setSelectedIntegrationId = useCallback((id: number | null) => {
    setSelectedIntegrationIdState(id)
    if (id) localStorage.setItem("centralops_integration_id", String(id))
    else localStorage.removeItem("centralops_integration_id")
  }, [])

  const clearFilters = useCallback(() => {
    setSelectedOrgIdState(null)
    setSelectedPlatformState(null)
    setSelectedIntegrationIdState(null)
    localStorage.removeItem("centralops_org_id")
    localStorage.removeItem("centralops_platform")
    localStorage.removeItem("centralops_integration_id")
  }, [])

  const refreshData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [orgs, ints] = await Promise.all([
        api.listOrganizations(),
        api.listIntegrations(),
      ])
      setOrganizations(orgs)
      setIntegrations(ints.filter((integration) => integration.is_active))
    } catch (cause) {
      // O provider só monta pós-autenticação (ProtectedLayout), então uma falha
      // aqui é erro real de rede/servidor — expõe estado para o GlobalFilters
      // oferecer retry, em vez de degradar para selects vazios silenciosos.
      setError(cause instanceof Error ? cause.message : "Falha ao carregar organizações e integrações.")
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    refreshData()
  }, [refreshData])

  const selectedOrganization = useMemo(
    () => organizations.find((o) => o.id === selectedOrgId) ?? null,
    [organizations, selectedOrgId],
  )

  const selectedIntegration = useMemo(
    () => integrations.find((i) => i.id === selectedIntegrationId) ?? null,
    [integrations, selectedIntegrationId],
  )

  // A seleção pertence a UM usuário, e o localStorage não sabe disso.
  //
  // O sintoma: um admin navega para a org 5, cria um operator escopado à org 8
  // e loga como ele NA MESMA ABA. `selectedOrgId` continua 5, `DashboardPage`
  // envia `organization_id: 5`, e o backend responde 403 "You do not have
  // access to this organization" — sobre uma org que o operator de fato não
  // pode ver, enquanto a org DELE estava correta o tempo todo.
  //
  // Marcar o DONO da seleção é o que resolve. Conferir contra a lista de
  // organizações carregada seria um proxy ruim, com dois falso-positivos caros:
  //   - `listOrganizations()` é chamado sem paginação e o backend usa `size=50`
  //     por padrão (`routers/organizations.py`). Num MSP com mais de 50 orgs,
  //     uma org válida fora da primeira página não estaria na lista, e a
  //     seleção seria apagada a cada montagem do provider.
  //   - Quando o fetch FALHA a lista também fica vazia, o que é ausência de
  //     informação, não prova de que a org sumiu.
  // A identidade do dono não sofre de nenhum dos dois: ou é o mesmo usuário, ou
  // não é.
  //
  // Limpa também quando a marca está AUSENTE (build anterior a esta, que já
  // podia ter deixado seleção salva): custa uma re-seleção logo após o deploy,
  // contra o risco de reproduzir o 403 que trava o dashboard.
  useEffect(() => {
    if (!userId) return
    if (localStorage.getItem(SCOPE_OWNER_KEY) === userId) return
    localStorage.setItem(SCOPE_OWNER_KEY, userId)
    clearFilters()
  }, [userId, clearFilters])

  useEffect(() => {
    if (selectedIntegrationId && !integrations.some((integration) => integration.id === selectedIntegrationId)) {
      setSelectedIntegrationId(null)
    }
  }, [integrations, selectedIntegrationId, setSelectedIntegrationId])

  const filteredIntegrations = useMemo(() => {
    let result = integrations
    if (selectedOrgId) {
      result = result.filter((i) => i.organization_id === selectedOrgId)
    }
    if (selectedPlatform) {
      result = result.filter((i) => i.platform === selectedPlatform)
    }
    return result
  }, [integrations, selectedOrgId, selectedPlatform])

  useEffect(() => {
    if (selectedIntegrationId && !filteredIntegrations.some((integration) => integration.id === selectedIntegrationId)) {
      setSelectedIntegrationId(null)
    }
  }, [filteredIntegrations, selectedIntegrationId, setSelectedIntegrationId])

  const value = useMemo(
    () => ({
      organizations,
      integrations,
      loading,
      error,
      selectedOrgId,
      selectedPlatform,
      selectedIntegrationId,
      setSelectedOrgId,
      setSelectedPlatform,
      setSelectedIntegrationId,
      selectedOrganization,
      selectedIntegration,
      filteredIntegrations,
      refreshData,
      clearFilters,
    }),
    [
      organizations, integrations, loading, error,
      selectedOrgId, selectedPlatform, selectedIntegrationId,
      setSelectedOrgId, setSelectedPlatform, setSelectedIntegrationId,
      selectedOrganization, selectedIntegration, filteredIntegrations,
      refreshData, clearFilters,
    ],
  )

  return <PlatformContext.Provider value={value}>{children}</PlatformContext.Provider>
}

export function usePlatform() {
  const ctx = useContext(PlatformContext)
  if (!ctx) throw new Error("usePlatform must be used within PlatformProvider")
  return ctx
}
