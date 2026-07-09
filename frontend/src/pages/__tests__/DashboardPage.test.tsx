import { render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import DashboardPage from "@/pages/DashboardPage"
import * as api from "@/services/api"
import { usePlatform } from "@/contexts/PlatformContext"

vi.mock("@/services/api")
vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: vi.fn(),
}))

const mockedApi = vi.mocked(api)
const mockedUsePlatform = vi.mocked(usePlatform)

function _mockPlatformContext(overrides: Partial<ReturnType<typeof usePlatform>> = {}): ReturnType<typeof usePlatform> {
  return {
    organizations: [],
    integrations: [],
    loading: false,
    error: null,
    selectedOrgId: null,
    selectedPlatform: null,
    selectedIntegrationId: null,
    setSelectedOrgId: vi.fn(),
    setSelectedPlatform: vi.fn(),
    setSelectedIntegrationId: vi.fn(),
    selectedOrganization: null,
    selectedIntegration: null,
    filteredIntegrations: [],
    refreshData: vi.fn(),
    clearFilters: vi.fn(),
    ...overrides,
  }
}

describe("DashboardPage", () => {
  it("usa o escopo global e renderiza buckets estratégicos", async () => {
    mockedUsePlatform.mockReturnValue({
      organizations: [],
      integrations: [],
      loading: false,
      selectedOrgId: 10,
      selectedPlatform: "wazuh",
      selectedIntegrationId: null,
      setSelectedOrgId: vi.fn(),
      setSelectedPlatform: vi.fn(),
      setSelectedIntegrationId: vi.fn(),
      selectedOrganization: { id: 10, name: "Org Alpha", slug: "org-alpha", is_active: true, integration_count: 2 },
      selectedIntegration: null,
      filteredIntegrations: [],
      refreshData: vi.fn(),
      clearFilters: vi.fn(),
    })

    mockedApi.getDashboardSummary.mockResolvedValue({
      organizations: { total: 1, active: 1 },
      integrations: {
        total: 2,
        active: 2,
        authenticated: 2,
        by_platform: { wazuh: 2 },
        health: { healthy: 2, degraded: 0, error: 0, unknown: 0 },
        degraded_items: [],
        comparison: {
          degraded_integrations: { current: 0, previous: 1, delta: -1, trend: "down" },
        },
      },
      alerts: {
        total: 12,
        by_severity: { critical: 4, high: 3, medium: 2, low: 2, info: 1 },
        trend: [],
        sources: [
          {
            integration_id: 100,
            integration_name: "Wazuh Prod",
            organization_id: 10,
            organization_name: "Org Alpha",
            total: 9,
            by_severity: { critical: 4, high: 2, medium: 1, low: 1, info: 1 },
          },
        ],
        top_hosts: [{ key: "srv-web-01", count: 4, integration_id: 100, integration_name: "Wazuh Prod", organization_id: 10, organization_name: "Org Alpha" }],
        top_rules: [{ key: "5710", label: "Suspicious login", count: 4, integration_id: 100, integration_name: "Wazuh Prod", organization_id: 10, organization_name: "Org Alpha" }],
        top_mitre_ids: [{ key: "T1110", count: 4, integration_id: 100, integration_name: "Wazuh Prod", organization_id: 10, organization_name: "Org Alpha" }],
        top_agent_groups: [{ key: "linux", count: 5, integration_id: 100, integration_name: "Wazuh Prod", organization_id: 10, organization_name: "Org Alpha" }],
        partial_errors: [],
        latest_timestamp: "2026-01-08T00:00:00Z",
        last_query_at: "2026-01-08T00:00:00Z",
        unsupported_sources: 0,
        window_days: 7,
        applied_organization_id: 10,
        applied_integration_id: null,
        applied_platform: "wazuh",
        comparison: {
          total_alerts: { current: 12, previous: 5, delta: 7, trend: "up" },
          critical_alerts: { current: 4, previous: 1, delta: 3, trend: "up" },
        },
        most_critical_client: { organization_id: 10, organization_name: "Org Alpha", integration_id: null, integration_name: null, critical: 4, high: 2, total: 9 },
        most_critical_integration: { organization_id: 10, organization_name: "Org Alpha", integration_id: 100, integration_name: "Wazuh Prod", critical: 4, high: 2, total: 9 },
      },
    })

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Escopo atual")).toBeInTheDocument()
    expect(screen.getByText("MITRE mais frequente")).toBeInTheDocument()
    expect(screen.getByText("Grupos de agentes mais afetados")).toBeInTheDocument()

    await waitFor(() => {
      expect(mockedApi.getDashboardSummary).toHaveBeenCalledWith({
        organization_id: 10,
        integration_id: null,
        platform: "wazuh",
        days: 7,
      })
    })
  })

  it("card Integrações no escopo exibe contagem de inativas quando health.inactive está presente", async () => {
    mockedUsePlatform.mockReturnValue(_mockPlatformContext())

    mockedApi.getDashboardSummary.mockResolvedValue({
      organizations: { total: 5, active: 5 },
      integrations: {
        total: 10,
        active: 8,
        authenticated: 8,
        by_platform: { wazuh: 6, sophos: 4 },
        health: { healthy: 5, degraded: 1, error: 0, unknown: 1, inactive: 2 },
        degraded_items: [],
        comparison: {
          degraded_integrations: { current: 1, previous: 0, delta: 1, trend: "up" },
        },
      },
      alerts: {
        total: 0,
        by_severity: { critical: 0, high: 0, medium: 0, low: 0, info: 0 },
        trend: [],
        sources: [],
        top_hosts: [],
        top_rules: [],
        top_mitre_ids: [],
        top_agent_groups: [],
        partial_errors: [],
        latest_timestamp: null,
        last_query_at: null,
        unsupported_sources: 0,
        window_days: 7,
        applied_organization_id: null,
        applied_integration_id: null,
        applied_platform: null,
        comparison: {
          total_alerts: { current: 0, previous: 0, delta: 0, trend: "stable" },
          critical_alerts: { current: 0, previous: 0, delta: 0, trend: "stable" },
        },
        most_critical_client: null,
        most_critical_integration: null,
      },
    })

    // getDashboardSummaryV2 rejeita → summaryV2 fica null → stats usa o v1
    mockedApi.getDashboardSummaryV2.mockRejectedValue(new Error("not used"))

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    await waitFor(() => {
      const card = screen.getByText(/Integrações no escopo/i)
      expect(card).toBeInTheDocument()
    })

    // O sub do card deve conter o breakdown com 2 inativas
    expect(await screen.findByText(/2 inativas/i)).toBeInTheDocument()
  })

  it("card Integrações no escopo usa fallback de inativas quando health.inactive está ausente", async () => {
    mockedUsePlatform.mockReturnValue(_mockPlatformContext())

    mockedApi.getDashboardSummary.mockResolvedValue({
      organizations: { total: 3, active: 3 },
      integrations: {
        total: 5,
        active: 3,
        authenticated: 3,
        by_platform: { wazuh: 5 },
        // sem campo inactive — fallback = total - active = 2
        health: { healthy: 3, degraded: 0, error: 0, unknown: 0 },
        degraded_items: [],
        comparison: {
          degraded_integrations: { current: 0, previous: 0, delta: 0, trend: "stable" },
        },
      },
      alerts: {
        total: 0,
        by_severity: { critical: 0, high: 0, medium: 0, low: 0, info: 0 },
        trend: [],
        sources: [],
        top_hosts: [],
        top_rules: [],
        top_mitre_ids: [],
        top_agent_groups: [],
        partial_errors: [],
        latest_timestamp: null,
        last_query_at: null,
        unsupported_sources: 0,
        window_days: 7,
        applied_organization_id: null,
        applied_integration_id: null,
        applied_platform: null,
        comparison: {
          total_alerts: { current: 0, previous: 0, delta: 0, trend: "stable" },
          critical_alerts: { current: 0, previous: 0, delta: 0, trend: "stable" },
        },
        most_critical_client: null,
        most_critical_integration: null,
      },
    })

    mockedApi.getDashboardSummaryV2.mockRejectedValue(new Error("not used"))

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(screen.getByText(/Integrações no escopo/i)).toBeInTheDocument()
    })

    // fallback: total(5) - active(3) = 2
    expect(await screen.findByText(/2 inativas/i)).toBeInTheDocument()
  })
})
