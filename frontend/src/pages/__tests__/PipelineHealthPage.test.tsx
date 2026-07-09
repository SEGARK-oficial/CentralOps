/**
 * Testes de componente — PipelineHealthPage.
 *
 * Cobre:
 *   - render padrão: grid de integrações com health
 *   - filtros all/healthy/problem
 *   - seção Saúde por destino: toggle oculto/visível
 *   - seção Saúde por destino: exibe card por destino
 *   - seção Saúde por destino: loading/empty/erro
 *   - acessibilidade: aria-pressed, aria-expanded, aria-controls
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import PipelineHealthPage from "@/pages/PipelineHealthPage"
import * as api from "@/services/api"
import type {
  Destination,
  DestinationHealth,
  Integration,
  IntegrationPipelineHealth,
} from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// ── fixtures ───────────────────────────────────────────────────────────────────

const integrationA: Integration = {
  id: 1,
  organization_id: 10,
  organization_name: "Org Alpha",
  name: "Wazuh Alpha",
  platform: "wazuh",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  capabilities: [],
}

const integrationB: Integration = {
  id: 2,
  organization_id: 10,
  organization_name: "Org Alpha",
  name: "Sophos Beta",
  platform: "sophos",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  capabilities: [],
}

const healthA: IntegrationPipelineHealth = {
  integration_id: 1,
  status: "healthy",
  events_per_minute: 10,
  lag_seconds: 5,
  last_error: null,
  last_success_at: "2026-06-17T00:00:00Z",
  mapped_field_ratio: 0.95,
  drift_count_24h: 0,
  quarantine_count_24h: 0,
  cached_at: "2026-06-17T00:00:00Z",
}

const healthB: IntegrationPipelineHealth = {
  integration_id: 2,
  status: "degraded",
  events_per_minute: 2,
  lag_seconds: 300,
  last_error: "timeout",
  last_success_at: "2026-06-17T00:00:00Z",
  mapped_field_ratio: 0.6,
  drift_count_24h: 5,
  quarantine_count_24h: 2,
  cached_at: "2026-06-17T00:00:00Z",
}

const dest1: Destination = {
  id: "dest-01",
  name: "Splunk Prod",
  kind: "splunk_hec",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: 10,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  has_secret: true,
}

const destHealth1: DestinationHealth = {
  destination_id: "dest-01",
  status: "healthy",
  enabled: true,
  breaker_state: "closed",
  dlq_total: 0,
  dlq_24h: 0,
  last_dlq_at: null,
  eps: 8,
  bytes_per_min: 4096,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <PipelineHealthPage />
    </MemoryRouter>,
  )
}

describe("PipelineHealthPage", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listIntegrations.mockResolvedValue([integrationA, integrationB])
    mockedApi.listPipelineHealth.mockResolvedValue([healthA, healthB])
    mockedApi.listDestinations.mockResolvedValue([dest1])
    mockedApi.getDestinationHealth.mockResolvedValue(destHealth1)
  })

  it("exibe integrações com health cards", async () => {
    renderPage()
    expect(await screen.findByText("Wazuh Alpha")).toBeInTheDocument()
    expect(screen.getByText("Sophos Beta")).toBeInTheDocument()
  })

  it("filtro 'Saudáveis' exibe apenas integração healthy", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    fireEvent.click(screen.getByRole("button", { name: /Saudáveis/i }))
    await waitFor(() => {
      expect(screen.queryByText("Sophos Beta")).not.toBeInTheDocument()
    })
    expect(screen.getByText("Wazuh Alpha")).toBeInTheDocument()
  })

  it("filtro 'Com problema' exibe apenas integração degraded", async () => {
    renderPage()
    await screen.findByText("Sophos Beta")
    fireEvent.click(screen.getByRole("button", { name: /Com problema/i }))
    await waitFor(() => {
      expect(screen.queryByText("Wazuh Alpha")).not.toBeInTheDocument()
    })
    expect(screen.getByText("Sophos Beta")).toBeInTheDocument()
  })

  // ── Seção Saúde por destino ────────────────────────────────────────────────

  it("seção de destinos começa oculta", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    expect(screen.queryByTestId("destination-health-grid")).not.toBeInTheDocument()
    expect(screen.queryByTestId("destinations-grid-empty")).not.toBeInTheDocument()
  })

  it("botão 'Ver destinos' exibe a seção", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    fireEvent.click(screen.getByRole("button", { name: /Ver destinos/i }))
    expect(await screen.findByTestId("destination-health-grid")).toBeInTheDocument()
    expect(screen.getByText("Splunk Prod")).toBeInTheDocument()
  })

  it("botão alterna aria-expanded ao toggle", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    const btn = screen.getByRole("button", { name: /Ver destinos/i })
    expect(btn).toHaveAttribute("aria-expanded", "false")
    fireEvent.click(btn)
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Ocultar destinos/i })).toHaveAttribute(
        "aria-expanded",
        "true",
      )
    })
  })

  it("seção tem aria-controls apontando para o grid", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    const btn = screen.getByRole("button", { name: /Ver destinos/i })
    expect(btn).toHaveAttribute("aria-controls", "dest-health-grid")
  })

  it("destino mostra EPS na seção de destinos", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    fireEvent.click(screen.getByRole("button", { name: /Ver destinos/i }))
    await screen.findByText("Splunk Prod")
    const epsCell = screen.getByTestId("dest-eps-dest-01")
    expect(epsCell.textContent).toBe("8")
  })

  it("exibe estado vazio quando sem destinos", async () => {
    mockedApi.listDestinations.mockResolvedValue([])
    renderPage()
    await screen.findByText("Wazuh Alpha")
    fireEvent.click(screen.getByRole("button", { name: /Ver destinos/i }))
    expect(await screen.findByTestId("destinations-grid-empty")).toBeInTheDocument()
    expect(screen.getByText(/Nenhum destino configurado/)).toBeInTheDocument()
  })

  it("exibe Notice de erro quando listDestinations falha", async () => {
    mockedApi.listDestinations.mockRejectedValue(new Error("403"))
    renderPage()
    await screen.findByText("Wazuh Alpha")
    fireEvent.click(screen.getByRole("button", { name: /Ver destinos/i }))
    expect(await screen.findByText("Falha ao carregar destinos")).toBeInTheDocument()
  })

  it("botão 'Ocultar destinos' esconde o grid novamente", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    fireEvent.click(screen.getByRole("button", { name: /Ver destinos/i }))
    await screen.findByTestId("destination-health-grid")
    fireEvent.click(screen.getByRole("button", { name: /Ocultar destinos/i }))
    await waitFor(() => {
      expect(screen.queryByTestId("destination-health-grid")).not.toBeInTheDocument()
    })
  })

  // ── Acessibilidade ──────────────────────────────────────────────────────────

  it("heading 'Saúde por destino' está presente", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    expect(screen.getByRole("heading", { name: "Saúde por destino" })).toBeInTheDocument()
  })

  it("filtros têm aria-pressed correto", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    const allBtn = screen.getByRole("button", { name: /Todos/i })
    expect(allBtn).toHaveAttribute("aria-pressed", "true")
    const healthyBtn = screen.getByRole("button", { name: /Saudáveis/i })
    expect(healthyBtn).toHaveAttribute("aria-pressed", "false")
  })
})
