/**
 * PlatformContext
 * Global context for organization, platform, and integration selection.
 * Persists selections to localStorage for session continuity.
 */

import type React from "react"
import { createContext, useContext, useCallback, useEffect, useState, useMemo } from "react"
import type { Integration, Organization, PlatformType } from "@/types"
import * as api from "@/services/api"

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

export const PlatformProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
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
