/**
 * Testes de componente — DestinationHealthGrid.
 *
 * Cobre:
 *   - render padrão: estado loading (skeletons)
 *   - estado pronto: exibe card por destino com métricas
 *   - estado vazio: nenhum destino configurado
 *   - erro: Notice + Tentar novamente
 *   - health ausente: exibe "—" nas métricas
 *   - circuit breaker: exibe estado do circuito
 *   - acessibilidade: grid aria-label, cards role=article
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { DestinationHealthGrid } from "@/components/health/DestinationHealthGrid"
import * as api from "@/services/api"
import type { Destination, DestinationHealth } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const dest1: Destination = {
  id: "dest-a1",
  name: "Splunk HEC Prod",
  kind: "splunk_hec",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  has_secret: true,
}

const dest2: Destination = {
  id: "dest-b2",
  name: "Elasticsearch Cold",
  kind: "elasticsearch",
  enabled: false,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  has_secret: false,
}

const health1: DestinationHealth = {
  destination_id: "dest-a1",
  status: "healthy",
  enabled: true,
  breaker_state: "closed",
  dlq_total: 0,
  dlq_24h: 0,
  last_dlq_at: null,
  eps: 42,
  bytes_per_min: 12800,
}

const health2: DestinationHealth = {
  destination_id: "dest-b2",
  status: "degraded",
  enabled: false,
  breaker_state: "open",
  dlq_total: 5,
  dlq_24h: 3,
  last_dlq_at: "2026-06-17T10:00:00Z",
  eps: null,
  bytes_per_min: null,
}

function renderGrid() {
  return render(
    <MemoryRouter>
      <DestinationHealthGrid />
    </MemoryRouter>,
  )
}

describe("DestinationHealthGrid", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listDestinations.mockResolvedValue([dest1, dest2])
    mockedApi.getDestinationHealth.mockImplementation((id: string) => {
      if (id === "dest-a1") return Promise.resolve(health1)
      if (id === "dest-b2") return Promise.resolve(health2)
      return Promise.reject(new Error("not found"))
    })
  })

  it("exibe skeletons durante carregamento", () => {
    mockedApi.listDestinations.mockReturnValue(new Promise(() => {}))
    renderGrid()
    expect(screen.getByRole("status", { name: "Carregando destinos…" })).toBeInTheDocument()
  })

  it("renderiza um card por destino com nome e kind", async () => {
    renderGrid()
    expect(await screen.findByText("Splunk HEC Prod")).toBeInTheDocument()
    expect(screen.getByText("Elasticsearch Cold")).toBeInTheDocument()
    expect(screen.getByText("splunk_hec")).toBeInTheDocument()
    expect(screen.getByText("elasticsearch")).toBeInTheDocument()
  })

  it("exibe EPS corretamente para destino saudável", async () => {
    renderGrid()
    await screen.findByText("Splunk HEC Prod")
    const epsCell = screen.getByTestId("dest-eps-dest-a1")
    expect(epsCell.textContent).toBe("42")
  })

  it("exibe — para EPS quando health.eps é null", async () => {
    renderGrid()
    await screen.findByText("Elasticsearch Cold")
    const epsCell = screen.getByTestId("dest-eps-dest-b2")
    expect(epsCell.textContent).toBe("—")
  })

  it("exibe DLQ (24h) corretamente", async () => {
    renderGrid()
    await screen.findByText("Splunk HEC Prod")
    // dest1: dlq_24h=0, dest2: dlq_24h=3
    const dlqValues = screen.getAllByText("0")
    expect(dlqValues.length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText("3")).toBeInTheDocument()
  })

  it("exibe breaker_state 'closed' como 'Circuito fechado'", async () => {
    renderGrid()
    await screen.findByText("Splunk HEC Prod")
    expect(screen.getByText("Circuito fechado")).toBeInTheDocument()
  })

  it("exibe breaker_state 'open' como 'Circuito aberto'", async () => {
    renderGrid()
    await screen.findByText("Elasticsearch Cold")
    expect(screen.getByText("Circuito aberto")).toBeInTheDocument()
  })

  it("exibe aviso de destino desabilitado", async () => {
    renderGrid()
    await screen.findByText("Elasticsearch Cold")
    expect(screen.getByText("Destino desabilitado")).toBeInTheDocument()
  })

  it("exibe — nas métricas quando getDestinationHealth falha (best-effort)", async () => {
    mockedApi.getDestinationHealth.mockRejectedValue(new Error("504"))
    renderGrid()
    await screen.findByText("Splunk HEC Prod")
    // sem health, EPS deve ser "—"
    const epsCell = screen.getByTestId("dest-eps-dest-a1")
    expect(epsCell.textContent).toBe("—")
  })

  it("estado vazio: exibe mensagem quando nenhum destino configurado", async () => {
    mockedApi.listDestinations.mockResolvedValue([])
    renderGrid()
    expect(await screen.findByTestId("destinations-grid-empty")).toBeInTheDocument()
    expect(screen.getByText(/Nenhum destino configurado/)).toBeInTheDocument()
  })

  it("estado de erro: exibe Notice + Tentar novamente", async () => {
    mockedApi.listDestinations.mockRejectedValue(new Error("Network error"))
    renderGrid()
    expect(await screen.findByText("Falha ao carregar destinos")).toBeInTheDocument()
    expect(screen.getByText("Network error")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Tentar novamente/i })).toBeInTheDocument()
  })

  it("Tentar novamente recarrega dados após erro", async () => {
    mockedApi.listDestinations
      .mockRejectedValueOnce(new Error("timeout"))
      .mockResolvedValue([dest1])
    mockedApi.getDestinationHealth.mockResolvedValue(health1)
    renderGrid()
    await screen.findByText("Falha ao carregar destinos")
    fireEvent.click(screen.getByRole("button", { name: /Tentar novamente/i }))
    expect(await screen.findByText("Splunk HEC Prod")).toBeInTheDocument()
  })

  // ── Acessibilidade ─────────────────────────────────────────────────────────

  it("grid tem aria-label 'Saúde por destino'", async () => {
    renderGrid()
    const grid = await screen.findByTestId("destination-health-grid")
    expect(grid).toBeInTheDocument()
    expect(grid).toHaveAttribute("aria-label", "Saúde por destino")
  })

  it("cada card tem role=article com aria-label do nome do destino", async () => {
    renderGrid()
    await screen.findByText("Splunk HEC Prod")
    const article = screen.getByRole("article", { name: /Destino Splunk HEC Prod/i })
    expect(article).toBeInTheDocument()
  })

  it("circuit breaker tem aria-label descritivo", async () => {
    renderGrid()
    await screen.findByText("Splunk HEC Prod")
    expect(screen.getByLabelText(/Circuit breaker: Circuito fechado/i)).toBeInTheDocument()
  })
})
