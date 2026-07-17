import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import IntegrationDetailPage from "@/pages/IntegrationDetailPage"
import * as api from "@/services/api"
import { useAuth } from "@/contexts/AuthContext"
import { usePlatform } from "@/contexts/PlatformContext"
import i18n from "@/i18n"
import type { Destination, IntegrationPipelineHealth, Route as AppRoute } from "@/types"

// jsdom's default navigator.language is "en-US", which the app's language
// detector picks up over the pt fallback — force pt here so assertions below
// (written against the PT catalog copy) match what a PT-BR user actually sees.
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: vi.fn(),
}))
vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: vi.fn(),
}))

const mockedApi = vi.mocked(api)
const mockedUseAuth = vi.mocked(useAuth)
const mockedUsePlatform = vi.mocked(usePlatform)

const PIPELINE_HEALTH: IntegrationPipelineHealth = {
  integration_id: 100,
  status: "healthy",
  events_per_minute: 15,
  lag_seconds: 10,
  last_error: null,
  last_success_at: "2026-04-25T10:00:00Z",
  mapped_field_ratio: 0.9,
  drift_count_24h: 0,
  quarantine_count_24h: 0,
  cached_at: new Date().toISOString(),
}

const integration = {
  id: 100,
  organization_id: 1,
  organization_name: "Org One",
  name: "Wazuh Prod",
  platform: "wazuh" as const,
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy" as const,
  capabilities: ["health:check"],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/integrations/100"]}>
      <Routes>
        <Route path="/integrations/:id" element={<IntegrationDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe("IntegrationDetailPage", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: { role: "admin" },
    } as never)
    mockedUsePlatform.mockReturnValue({
      setSelectedIntegrationId: vi.fn(),
    } as never)
    mockedApi.getIntegration.mockResolvedValue(integration as never)
    mockedApi.getIntegrationPipelineHealth.mockResolvedValue(PIPELINE_HEALTH as never)
    // a página passou a carregar o catálogo de query-capabilities no mount.
    mockedApi.listQueryCapabilities.mockResolvedValue([])
  })

  it("aba Saúde de Normalização: exibe IntegrationHealthPanel com métricas", async () => {
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration,
      health: null,
    } as never)

    renderPage()

    // Aguarda página carregar
    await screen.findByText("Wazuh Prod")

    // Clica na aba Saúde de Normalização
    fireEvent.click(screen.getByRole("tab", { name: /saúde de normalização/i }))

    // Aguarda o painel de saúde aparecer
    await waitFor(() => {
      expect(screen.getByTestId("integration-health-panel")).toBeInTheDocument()
    })

    // Verifica que API foi chamada com o id correto
    expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledWith(
      100,
      expect.any(Object),
    )

    // Verifica métricas renderizadas
    await waitFor(() => {
      expect(screen.getByTestId("metrics-events-per-minute")).toBeInTheDocument()
    })
    expect(screen.getByTestId("health-refresh-button")).toBeInTheDocument()
  })

  it("last_error exibe details colapsado por padrão e expande ao clicar", async () => {
    mockedApi.getIntegration.mockResolvedValue({
      ...integration,
      last_error: "Connection refused: timeout after 30s",
    } as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration,
      health: null,
    } as never)

    renderPage()
    await screen.findByText("Wazuh Prod")

    const details = document.querySelector("details") as HTMLDetailsElement
    expect(details).toBeInTheDocument()
    expect(details.open).toBe(false)

    const summary = details.querySelector("summary") as HTMLElement
    expect(summary.textContent).toContain("Ver erro técnico da última verificação")

    expect(screen.queryByText("Connection refused: timeout after 30s")).not.toBeVisible()

    fireEvent.click(summary)
    expect(details.open).toBe(true)
    expect(screen.getByText("Connection refused: timeout after 30s")).toBeVisible()
  })

  it("sem last_error não renderiza o details", async () => {
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration,
      health: null,
    } as never)

    renderPage()
    await screen.findByText("Wazuh Prod")

    expect(screen.queryByText("Ver erro técnico da última verificação")).not.toBeInTheDocument()
  })

  // ── Produtos licenciados ────────────────────────────────────────────────────

  const sophosChildIntegration = {
    ...integration,
    platform: "sophos" as const,
    kind: "tenant" as const,
    parent_integration_id: 5,
    name: "Sophos Child Tenant",
  }

  it("renderiza seção Produtos licenciados quando licensed_products tem itens", async () => {
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: [
        {
          code: "CIXA-MSP",
          label: "Sophos Endpoint - User MSP Monthly",
          category: null,
          details: { quantity: 50, endDate: "2026-12-31", type: "usage" },
        },
        { code: "CIXAXDR", label: "Sophos XDR - User", category: "xdr", details: {} },
      ],
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.getByText("Produtos licenciados")).toBeInTheDocument()
    // chips dos produtos (tooltip novo formato)
    expect(
      screen.getByLabelText(/Sophos Endpoint - User MSP Monthly — Qtd: 50 · Validade: 2026-12-31 · Tipo: usage/),
    ).toBeInTheDocument()
    expect(screen.getByLabelText("Sophos XDR - User")).toBeInTheDocument()
    // resumo de capabilities — XDR licenciado, MDR não
    expect(screen.getByText(/Detections API: licenciado \(via XDR\)/)).toBeInTheDocument()
    expect(screen.getByText(/Cases API \(MDR\): não licenciado/)).toBeInTheDocument()
  })

  it("mostra usage atual quando disponível e marca Detections via XDR", async () => {
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: [
        {
          code: "CIXAXDR",
          label: "Sophos XDR - User",
          category: "xdr",
          details: { quantity: 2000, usageCount: 1786, endDate: "2027-01-17", type: "enterprise" },
        },
      ],
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.getByLabelText(/Uso: 1786\/2000/)).toBeInTheDocument()
    expect(screen.getByText(/Detections API: licenciado \(via XDR\)/)).toBeInTheDocument()
    expect(screen.getByText(/Cases API \(MDR\): não licenciado/)).toBeInTheDocument()
  })

  it("marca Detections como licenciado via MDR quando só há licença MDR (sem XDR)", async () => {
    // Um tenant com MDR-COMPLETE + SVRMTR-ADV-ADDON mas sem CIXAXDR;
    // /detections/v1 ainda funciona porque o time SOC do MDR usa o mesmo endpoint.
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: [
        {
          code: "MDR-COMPLETE",
          label: "Sophos MDR Complete - User",
          category: "mdr",
          details: { quantity: 1012, usageCount: 1008, endDate: "2027-11-27", type: "enterprise" },
        },
        {
          code: "SVRMTR-ADV-ADDON",
          label: "Central MTR Advanced Add-on for Intercept X",
          category: "mdr",
          details: { quantity: 40, usageCount: 25 },
        },
      ],
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.getByText(/Detections API: licenciado \(via MDR\)/)).toBeInTheDocument()
    expect(screen.getByText(/Cases API \(MDR\): licenciado/)).toBeInTheDocument()
  })

  it("marca Detections como licenciado (XDR + MDR) quando ambos estão presentes", async () => {
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: [
        { code: "CIXAXDR", label: "Sophos XDR - User", category: "xdr", details: {} },
        { code: "MDR-COMPLETE", label: "Sophos MDR Complete", category: "mdr", details: {} },
      ],
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.getByText(/Detections API: licenciado \(XDR \+ MDR\)/)).toBeInTheDocument()
    expect(screen.getByText(/Cases API \(MDR\): licenciado/)).toBeInTheDocument()
  })

  it("marca Detections como NÃO licenciado quando não há XDR nem MDR", async () => {
    // Um tenant com 25 SKUs MSP mas nenhum XDR/MDR.
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: [
        { code: "CIXA-MSP", label: "Sophos Endpoint - User MSP Monthly", category: null, details: {} },
        { code: "CEMA-MSP", label: "Sophos Email MSP Monthly", category: null, details: {} },
      ],
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.getByText(/Detections API: não licenciado/)).toBeInTheDocument()
    expect(screen.getByText(/Cases API \(MDR\): não licenciado/)).toBeInTheDocument()
  })

  it("NÃO renderiza seção Produtos licenciados quando licensed_products é null", async () => {
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: null,
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.queryByText("Produtos licenciados")).not.toBeInTheDocument()
  })

  it("renderiza mensagem discreta quando licensed_products é array vazio", async () => {
    mockedApi.getIntegration.mockResolvedValue(sophosChildIntegration as never)
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration: sophosChildIntegration,
      health: null,
      licensed_products: [],
    } as never)

    renderPage()
    await screen.findByText("Sophos Child Tenant")

    expect(screen.getByText("Produtos licenciados")).toBeInTheDocument()
    expect(screen.getByText("Nenhum produto licenciado retornado pela API.")).toBeInTheDocument()
  })

  // ────────────────────────────────────────────────────────────────────────────

  // ── Aba Destinos ─────────────────────────────────────────────────────────────

  const routeWazuh: AppRoute = {
    id: "route-01",
    name: "Rota Wazuh",
    priority: 10,
    condition: { vendor: "wazuh" },
    action: "route",
    destination_ids: ["dest-x1"],
    is_final: false,
    canary_percent: 100,
    transform_ref: null,
    pii_redaction: null,
    enabled: true,
    organization_id: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    unreachable: false,
  }

  const destX1: Destination = {
    id: "dest-x1",
    name: "SIEM Alpha",
    kind: "splunk_hec",
    enabled: true,
    config: {},
    delivery: {},
    config_version: "1",
    organization_id: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    has_secret: false,
  }

  it("aba Destinos: exibe rota correspondente e destino derivado", async () => {
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration,
      health: null,
    } as never)
    mockedApi.listRoutes.mockResolvedValue([routeWazuh] as never)
    mockedApi.listDestinations.mockResolvedValue([destX1] as never)
    mockedApi.dryRunRoutes.mockResolvedValue({
      evaluated: 1,
      sample_source: "synthetic",
      routed: 1,
      dropped: 0,
      fallback: 0,
      per_destination: { "dest-x1": 1 },
      unreachable_route_ids: [],
      results: [],
    } as never)

    renderPage()
    await screen.findByText("Wazuh Prod")

    fireEvent.click(screen.getByRole("tab", { name: /^destinos$/i }))

    expect(await screen.findByText("Rota Wazuh")).toBeInTheDocument()
    expect(screen.getByText("SIEM Alpha")).toBeInTheDocument()
  })

  it("aba Destinos: estado vazio quando nenhuma rota corresponde", async () => {
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration,
      health: null,
    } as never)
    // rota de outra plataforma, não referencia wazuh
    mockedApi.listRoutes.mockResolvedValue([{
      ...routeWazuh,
      id: "route-99",
      name: "Rota Sophos",
      condition: { vendor: "sophos" },
    }] as never)
    mockedApi.listDestinations.mockResolvedValue([destX1] as never)
    mockedApi.dryRunRoutes.mockResolvedValue({
      evaluated: 0, sample_source: "synthetic", routed: 0, dropped: 0,
      fallback: 0, per_destination: {}, unreachable_route_ids: [], results: [],
    } as never)

    renderPage()
    await screen.findByText("Wazuh Prod")
    fireEvent.click(screen.getByRole("tab", { name: /^destinos$/i }))

    expect(
      await screen.findByText(/Nenhuma rota encontrada para esta integração/i),
    ).toBeInTheDocument()
  })

  it("aba Destinos: erro de API exibe Notice", async () => {
    mockedApi.getIntegrationOverview.mockResolvedValue({
      integration,
      health: null,
    } as never)
    mockedApi.listRoutes.mockRejectedValue(new Error("Network timeout"))
    mockedApi.listDestinations.mockResolvedValue([destX1] as never)

    renderPage()
    await screen.findByText("Wazuh Prod")
    fireEvent.click(screen.getByRole("tab", { name: /^destinos$/i }))

    expect(await screen.findByText("Falha ao carregar destinos")).toBeInTheDocument()
    expect(screen.getByText("Network timeout")).toBeInTheDocument()
  })

  // ── fim aba Destinos ──────────────────────────────────────────────────────────
})
