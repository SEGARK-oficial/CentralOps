import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import AlertsPage from "@/pages/AlertsPage"
import * as api from "@/services/api"
import { usePlatform } from "@/contexts/PlatformContext"
import { DEFAULT_ALERT_INDEX } from "@/lib/alerts"

vi.mock("@/services/api")
vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: vi.fn(),
}))

const mockedApi = vi.mocked(api)
const mockedUsePlatform = vi.mocked(usePlatform)

describe("AlertsPage", () => {
  beforeEach(() => {
    mockedUsePlatform.mockReturnValue({
      organizations: [],
      integrations: [],
      loading: false,
      selectedOrgId: 1,
      selectedPlatform: "wazuh",
      selectedIntegrationId: 100,
      setSelectedOrgId: vi.fn(),
      setSelectedPlatform: vi.fn(),
      setSelectedIntegrationId: vi.fn(),
      selectedOrganization: null,
      selectedIntegration: null,
      filteredIntegrations: [
        {
          id: 100,
          organization_id: 1,
          name: "Wazuh Prod",
          platform: "wazuh",
          is_active: true,
          is_authenticated: true,
          auth_status: "healthy",
          capabilities: ["alerts:list", "alerts:detail"],
        },
      ],
      refreshData: vi.fn(),
      clearFilters: vi.fn(),
    })
  })

  it("mantém filtros avançados recolhidos por padrão e abre o drawer ao clicar na linha", async () => {
    mockedApi.listAlerts.mockResolvedValue({
      items: [
        {
          alert_id: "alert-1",
          title: "Suspicious login",
          severity: "high",
          platform: "wazuh",
          timestamp: "2026-01-08T00:00:00Z",
          hostname: "srv-web-01",
          rule_id: "5710",
          rule_level: 12,
          rule_groups: [],
          rule_firedtimes: 1,
          mitre_ids: [],
          mitre_tactics: [],
          mitre_techniques: [],
          agent_labels: {},
          data_fields: {},
          highlights: {},
          integration_id: 100,
          integration_name: "Wazuh Prod",
          source_index: "wazuh-alerts-4.x-2026.01.08",
          raw: {},
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
      has_more: false,
    })
    mockedApi.getAlertDetail.mockResolvedValue({
      alert_id: "alert-1",
      title: "Suspicious login",
      severity: "high",
      platform: "wazuh",
      timestamp: "2026-01-08T00:00:00Z",
      hostname: "srv-web-01",
      rule_id: "5710",
      rule_level: 12,
      rule_groups: [],
      rule_firedtimes: 1,
      mitre_ids: [],
      mitre_tactics: [],
      mitre_techniques: [],
      agent_labels: {},
      data_fields: {},
      highlights: {},
      integration_id: 100,
      integration_name: "Wazuh Prod",
      manager_name: "manager-01",
      source_index: "wazuh-alerts-4.x-2026.01.08",
      raw: {},
    })

    render(
      <MemoryRouter>
        <AlertsPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Suspicious login")).toBeInTheDocument()
    expect(screen.queryByLabelText("Consulta avançada")).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /filtros avançados/i }))
    expect(await screen.findByLabelText("Consulta avançada")).toBeInTheDocument()

    fireEvent.click(screen.getByText("Suspicious login"))
    expect(await screen.findByText("Alert ID")).toBeInTheDocument()
    expect(screen.getByText("manager-01")).toBeInTheDocument()
    expect(mockedApi.listAlerts).toHaveBeenCalledWith(
      100,
      expect.objectContaining({ index: DEFAULT_ALERT_INDEX, limit: 50 }),
      expect.any(Object),
    )
    expect(mockedApi.getAlertDetail).toHaveBeenCalledWith(
      100,
      "alert-1",
      { index: "wazuh-alerts-4.x-2026.01.08" },
      expect.any(Object),
    )
  })

  it("oculta os filtros específicos de indexer (índice + avançados) para fontes não-Wazuh", async () => {
    // Fonte Sophos: a taxonomia do indexer Wazuh (índice, rule/level/agent/decoder,
    // query-string) NÃO deve aparecer num SDPP neutro; os filtros universais ficam.
    mockedUsePlatform.mockReturnValue({
      organizations: [],
      integrations: [],
      loading: false,
      selectedOrgId: 1,
      selectedPlatform: "sophos",
      selectedIntegrationId: 200,
      setSelectedOrgId: vi.fn(),
      setSelectedPlatform: vi.fn(),
      setSelectedIntegrationId: vi.fn(),
      selectedOrganization: null,
      selectedIntegration: null,
      filteredIntegrations: [
        {
          id: 200,
          organization_id: 1,
          name: "Sophos Prod",
          platform: "sophos",
          is_active: true,
          is_authenticated: true,
          auth_status: "healthy",
          capabilities: ["alerts:list"],
        },
      ],
      refreshData: vi.fn(),
      clearFilters: vi.fn(),
    } as never)
    mockedApi.listAlerts.mockResolvedValue({
      items: [],
      total: 0,
      limit: 50,
      offset: 0,
      has_more: false,
    })
    // Limpa chamadas acumuladas do teste anterior (mocks persistem entre testes) para
    // que mock.calls[0] seja a chamada DESTE cenário (fonte Sophos).
    mockedApi.listAlerts.mockClear()

    render(
      <MemoryRouter>
        <AlertsPage />
      </MemoryRouter>,
    )

    await waitFor(() => expect(mockedApi.listAlerts).toHaveBeenCalled())

    // Camada de dados: a requisição p/ Sophos NÃO leva os campos do indexer Wazuh
    // (sem vazar index="wazuh-alerts-*" nem rule_id/etc. a um provedor não-Wazuh).
    const callFilters = mockedApi.listAlerts.mock.calls[0][1] as Record<string, unknown>
    expect(callFilters.index).toBeUndefined()
    expect(callFilters.rule_id).toBeUndefined()
    expect(callFilters.query).toBeUndefined()

    // UI específica de indexer Wazuh ausente para fonte não-Wazuh:
    expect(screen.queryByText("Índice")).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: /filtros avançados/i }),
    ).not.toBeInTheDocument()
    // Filtros universais permanecem:
    expect(screen.getByText("Hostname")).toBeInTheDocument()
  })
})
