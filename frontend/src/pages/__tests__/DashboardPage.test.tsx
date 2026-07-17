import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import DashboardPage from "@/pages/DashboardPage"
import * as api from "@/services/api"
import { usePlatform } from "@/contexts/PlatformContext"
import i18n from "@/i18n"
import type { DashboardSummaryV2 } from "@/types"

// jsdom's default navigator.language is "en-US"; force pt so catalog
// assertions below match the PT copy.
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: vi.fn(),
}))

const mockedApi = vi.mocked(api)
const mockedUsePlatform = vi.mocked(usePlatform)

function mockPlatformContext(overrides: Partial<ReturnType<typeof usePlatform>> = {}): ReturnType<typeof usePlatform> {
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

function buildSummary(overrides: Partial<DashboardSummaryV2> = {}): DashboardSummaryV2 {
  return {
    schema_version: 2,
    window: "7d",
    generated_at: "2026-07-15T12:00:00Z",
    kpis: [
      { id: "ingest_eps", label: "Ingestão (EPS)", value: 12.5, sub: "eventos/s", icon_id: "activity", severity: "ok" },
      { id: "quarantine_rate", label: "Quarentena 24h", value: "0.2%", sub: "taxa 24h", icon_id: "shield-alert", severity: "ok" },
    ],
    top_buckets: [
      {
        id: "top_sources_volume",
        label: "Top fontes por volume",
        icon_id: "activity",
        empty_hint: null,
        items: [{ id: "101", label: "Wazuh Lab", value: 42, sub: "Org Alpha" }],
      },
      {
        id: "top_quarantine",
        label: "Maiores quarentenas (24h)",
        icon_id: "shield-alert",
        empty_hint: "Sem eventos em quarentena nas últimas 24h.",
        items: [],
      },
    ],
    organizations: { total: 1, active: 1 },
    integrations: {
      total: 2,
      active: 2,
      authenticated: 2,
      by_platform: { wazuh: 2 },
      health: { healthy: 1, degraded: 1, error: 0, unknown: 0, inactive: 0 },
      degraded_items: [
        {
          integration_id: 100,
          integration_name: "Wazuh Prod",
          organization_id: 10,
          organization_name: "Org Alpha",
          status: "degraded",
          last_error: "timeout",
          last_checked_at: "2026-07-15T11:55:00Z",
        },
      ],
      comparison: {
        degraded_integrations: { current: 1, previous: 0, delta: 1, trend: "up" },
      },
    },
    ...overrides,
  }
}

describe("DashboardPage", () => {
  it("faz UMA chamada consolidada com o escopo global e renderiza KPIs + buckets", async () => {
    mockedUsePlatform.mockReturnValue(
      mockPlatformContext({
        selectedOrgId: 10,
        selectedPlatform: "wazuh",
        selectedOrganization: { id: 10, name: "Org Alpha", slug: "org-alpha", is_active: true, integration_count: 2 },
      }),
    )
    mockedApi.getDashboardSummary.mockResolvedValue(buildSummary())

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Escopo atual")).toBeInTheDocument()
    // KPIs data-driven do payload v2
    expect(screen.getByText("Ingestão (EPS)")).toBeInTheDocument()
    // Buckets data-driven do payload v2
    expect(screen.getByText("Top fontes por volume")).toBeInTheDocument()
    expect(screen.getByText("Maiores quarentenas (24h)")).toBeInTheDocument()

    await waitFor(() => {
      expect(mockedApi.getDashboardSummary).toHaveBeenCalledWith({
        organization_id: 10,
        integration_id: null,
        platform: "wazuh",
        days: 7,
      })
    })
    // Fetch ÚNICA — o dual-fetch v1+v2 foi consolidado
    expect(mockedApi.getDashboardSummary).toHaveBeenCalledTimes(1)
  })

  it("exibe integrações degradadas e distribuição por plataforma na seção Fontes e saúde", async () => {
    const setSelectedIntegrationId = vi.fn()
    mockedUsePlatform.mockReturnValue(mockPlatformContext({ setSelectedIntegrationId }))
    mockedApi.getDashboardSummary.mockResolvedValue(buildSummary())

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Fontes e saúde")).toBeInTheDocument()
    expect(screen.getByText("Wazuh Prod")).toBeInTheDocument()
    expect(screen.getByText("degraded")).toBeInTheDocument()
    expect(screen.getByText(/timeout/)).toBeInTheDocument()
    // by_platform
    expect(screen.getByText("Integrações por plataforma")).toBeInTheDocument()
    expect(screen.getByText("wazuh")).toBeInTheDocument()

    // clicar num item degradado seleciona a integração (deep-link p/ detalhe)
    fireEvent.click(screen.getByText("Wazuh Prod"))
    expect(setSelectedIntegrationId).toHaveBeenCalledWith(100)
  })

  it("mostra as contagens de escopo e o horário de geração no ScopeSummary", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())
    mockedApi.getDashboardSummary.mockResolvedValue(buildSummary())

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText(/Escopo: 1 cliente\(s\) · 2 integração\(ões\) \(2 ativas\)/)).toBeInTheDocument()
    expect(screen.getByText(/Dados gerados:/)).toBeInTheDocument()
  })
})
