/**
 * Testes de componente — IntegrationDestinationsTab.
 *
 * Cobre:
 *   - render padrão: estado loading (skeletons)
 *   - estado vazio: nenhuma rota corresponde
 *   - estado com rotas: lista rotas + destinos com nomes cruzados
 *   - dry-run exibe resultado (roteado / descartado)
 *   - erro de carregamento: exibe Notice + botão Tentar novamente
 *   - acessibilidade: Notice informativo sempre presente
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { IntegrationDestinationsTab } from "@/components/integrations/IntegrationDestinationsTab"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { Destination, Integration, Route, RouteDryRunResponse } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// jsdom's default navigator.language is "en-US", which the app's language
// detector picks up over the pt fallback — force pt here so assertions below
// (written against the PT catalog copy) match what a PT-BR user actually sees.
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const integration: Integration = {
  id: 42,
  organization_id: 1,
  organization_name: "Org Alpha",
  name: "Wazuh Prod",
  platform: "wazuh",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  capabilities: [],
}

const destination1: Destination = {
  id: "dest-01",
  name: "Splunk HEC",
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

const destination2: Destination = {
  id: "dest-02",
  name: "Elasticsearch",
  kind: "elasticsearch",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  has_secret: false,
}

const routeWazuh: Route = {
  id: "route-01",
  name: "Rota Wazuh SIEM",
  priority: 10,
  condition: { vendor: "wazuh" },
  action: "route",
  destination_ids: ["dest-01", "dest-02"],
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

const routeUnrelated: Route = {
  id: "route-99",
  name: "Rota Sophos",
  priority: 99,
  condition: { vendor: "sophos" },
  action: "route",
  destination_ids: ["dest-02"],
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

const dryRunResponse: RouteDryRunResponse = {
  evaluated: 1,
  sample_source: "synthetic",
  routed: 1,
  dropped: 0,
  fallback: 0,
  per_destination: { "dest-01": 1, "dest-02": 1 },
  unreachable_route_ids: [],
  results: [
    {
      labels: { vendor: "wazuh" },
      destinations: ["dest-01", "dest-02"],
      dropped: false,
      fallback: false,
    },
  ],
}

function renderTab() {
  return render(
    <MemoryRouter>
      <IntegrationDestinationsTab integration={integration} />
    </MemoryRouter>,
  )
}

describe("IntegrationDestinationsTab", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listRoutes.mockResolvedValue([routeWazuh, routeUnrelated])
    mockedApi.listDestinations.mockResolvedValue([destination1, destination2])
    mockedApi.dryRunRoutes.mockResolvedValue(dryRunResponse)
  })

  it("exibe aviso informativo sobre preview de roteamento", async () => {
    renderTab()
    expect(screen.getByText(/Preview de roteamento/i)).toBeInTheDocument()
  })

  it("exibe skeletons durante o carregamento", () => {
    // resolve nunca para manter estado loading
    mockedApi.listRoutes.mockReturnValue(new Promise(() => {}))
    mockedApi.listDestinations.mockReturnValue(new Promise(() => {}))
    renderTab()
    expect(screen.getByRole("status", { name: "Carregando destinos…" })).toBeInTheDocument()
  })

  it("renderiza rota correspondente com destinos (por vendor wazuh)", async () => {
    renderTab()
    expect(await screen.findByText("Rota Wazuh SIEM")).toBeInTheDocument()
    expect(screen.getByText("Splunk HEC")).toBeInTheDocument()
    expect(screen.getByText("Elasticsearch")).toBeInTheDocument()
  })

  it("NÃO exibe rota não relacionada (sophos)", async () => {
    renderTab()
    await screen.findByText("Rota Wazuh SIEM")
    expect(screen.queryByText("Rota Sophos")).not.toBeInTheDocument()
  })

  it("exibe resultado do dry-run: roteado + 2 destinos confirmados", async () => {
    renderTab()
    await screen.findByText("Rota Wazuh SIEM")
    expect(await screen.findByText("Roteado")).toBeInTheDocument()
    expect(screen.getByText(/2 destino\(s\) confirmado\(s\)/)).toBeInTheDocument()
  })

  it("exibe estado vazio quando nenhuma rota corresponde", async () => {
    // Apenas rota sophos, que não referencia wazuh
    mockedApi.listRoutes.mockResolvedValue([routeUnrelated])
    renderTab()
    expect(await screen.findByTestId("destinations-empty")).toBeInTheDocument()
    expect(screen.getByText(/Nenhuma rota encontrada para esta integração/)).toBeInTheDocument()
  })

  it("exibe Notice de erro e botão Tentar novamente quando listRoutes falha", async () => {
    mockedApi.listRoutes.mockRejectedValue(new Error("Timeout"))
    renderTab()
    expect(await screen.findByText("Falha ao carregar destinos")).toBeInTheDocument()
    expect(screen.getByText("Timeout")).toBeInTheDocument()
    const btn = screen.getByRole("button", { name: /Tentar novamente/i })
    expect(btn).toBeInTheDocument()
  })

  it("Tentar novamente recarrega os dados", async () => {
    mockedApi.listRoutes
      .mockRejectedValueOnce(new Error("Timeout"))
      .mockResolvedValue([routeWazuh])
    renderTab()
    await screen.findByText("Falha ao carregar destinos")
    fireEvent.click(screen.getByRole("button", { name: /Tentar novamente/i }))
    expect(await screen.findByText("Rota Wazuh SIEM")).toBeInTheDocument()
  })

  it("dry-run falha silenciosamente — ainda exibe rotas sem resultado de simulação", async () => {
    mockedApi.dryRunRoutes.mockRejectedValue(new Error("503"))
    renderTab()
    expect(await screen.findByText("Rota Wazuh SIEM")).toBeInTheDocument()
    // Sem resultado de simulação, não exibe badge "Roteado"
    expect(screen.queryByText("Roteado")).not.toBeInTheDocument()
  })

  it("exibe badge Final para rota is_final", async () => {
    mockedApi.listRoutes.mockResolvedValue([{ ...routeWazuh, is_final: true }])
    renderTab()
    expect(await screen.findByText("Final")).toBeInTheDocument()
  })

  it("exibe badge Desabilitada para rota desabilitada (enabled=false)", async () => {
    // rota desabilitada não aparece na lista pois filtramos r.enabled === true
    mockedApi.listRoutes.mockResolvedValue([{ ...routeWazuh, enabled: false }])
    renderTab()
    await waitFor(() => {
      expect(screen.queryByText("Rota Wazuh SIEM")).not.toBeInTheDocument()
    })
    expect(screen.getByTestId("destinations-empty")).toBeInTheDocument()
  })

  // ── Acessibilidade ──────────────────────────────────────────────────────────

  it("região de rotas tem aria-label descritivo", async () => {
    renderTab()
    expect(
      await screen.findByRole("list", { name: /Rotas correspondentes a esta integração/i }),
    ).toBeInTheDocument()
  })

  it("destinos da rota têm aria-label com o nome da rota", async () => {
    renderTab()
    await screen.findByText("Rota Wazuh SIEM")
    expect(
      screen.getByRole("list", { name: /Destinos da rota Rota Wazuh SIEM/i }),
    ).toBeInTheDocument()
  })
})
