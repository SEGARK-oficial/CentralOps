import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { IntegrationHealthPanel } from "@/components/health/IntegrationHealthPanel"
import * as api from "@/services/api"
import type { IntegrationPipelineHealth } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    getIntegrationPipelineHealth: vi.fn(),
  }
})

const mockedApi = vi.mocked(api)

const BASE_HEALTH: IntegrationPipelineHealth = {
  integration_id: 1,
  status: "healthy",
  events_per_minute: 42,
  lag_seconds: 30,
  last_error: null,
  last_success_at: "2026-04-25T10:00:00Z",
  mapped_field_ratio: 0.87,
  drift_count_24h: 5,
  quarantine_count_24h: 2,
  cached_at: new Date().toISOString(),
}

beforeEach(() => {
  vi.clearAllMocks()
})

function renderPanel(health: IntegrationPipelineHealth = BASE_HEALTH) {
  mockedApi.getIntegrationPipelineHealth.mockResolvedValue(health)
  return render(
    <MemoryRouter>
      <IntegrationHealthPanel integrationId={1} />
    </MemoryRouter>,
  )
}

describe("IntegrationHealthPanel", () => {
  it("renderiza data-testid principal", async () => {
    renderPanel()
    expect(screen.getByTestId("integration-health-panel")).toBeInTheDocument()
    await waitFor(() => expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalled())
  })

  it("exibe todas as métricas depois do load", async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByTestId("metrics-events-per-minute")).toBeInTheDocument())

    expect(screen.getByTestId("metrics-events-per-minute")).toHaveTextContent("42")
    expect(screen.getByTestId("metrics-lag")).toHaveTextContent("há 30s")
    expect(screen.getByTestId("metrics-drift-24h")).toHaveTextContent("5")
    expect(screen.getByTestId("metrics-quarantine-24h")).toHaveTextContent("2")
  })

  it("status unhealthy: badge Indisponível visível", async () => {
    renderPanel({ ...BASE_HEALTH, status: "unhealthy" })
    await waitFor(() => expect(screen.getByText("Indisponível")).toBeInTheDocument())
    expect(screen.getByTestId("health-status-card")).toBeInTheDocument()
  })

  it("last_error null: painel de erro não renderiza", async () => {
    renderPanel({ ...BASE_HEALTH, last_error: null })
    await waitFor(() => expect(screen.getByTestId("metrics-events-per-minute")).toBeInTheDocument())
    expect(screen.queryByText(/Último erro registrado/)).not.toBeInTheDocument()
  })

  it("last_error preenchido: exibe mensagem de erro", async () => {
    renderPanel({ ...BASE_HEALTH, last_error: "Connection timed out" })
    await waitFor(() => expect(screen.getByText("Último erro registrado")).toBeInTheDocument())
    expect(screen.getByText("Connection timed out")).toBeInTheDocument()
  })

  it("mapped_field_ratio null: exibe mensagem de cobertura não calculável", async () => {
    renderPanel({ ...BASE_HEALTH, mapped_field_ratio: null })
    await waitFor(() => expect(screen.getByText(/Cobertura não calculável/)).toBeInTheDocument())
  })

  it("mapped_field_ratio 0.87: exibe 87% e barra de progresso", async () => {
    renderPanel({ ...BASE_HEALTH, mapped_field_ratio: 0.87 })
    await waitFor(() => expect(screen.getByText("87%")).toBeInTheDocument())
    expect(screen.getByRole("progressbar")).toBeInTheDocument()
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "87")
  })

  it("refresh button chama refetch e tem aria-label", async () => {
    renderPanel()
    await waitFor(() => expect(screen.getByTestId("health-refresh-button")).toBeInTheDocument())

    const btn = screen.getByTestId("health-refresh-button")
    expect(btn).toHaveAttribute("aria-label", "Atualizar métricas de saúde")

    fireEvent.click(btn)

    await waitFor(() => expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(2))
  })

  it("exibe Notice de erro quando API falha", async () => {
    mockedApi.getIntegrationPipelineHealth.mockRejectedValue(new Error("API down"))
    render(
      <MemoryRouter>
        <IntegrationHealthPanel integrationId={1} />
      </MemoryRouter>,
    )
    await waitFor(() => expect(screen.getByText(/Falha ao carregar dados de saúde/)).toBeInTheDocument())
    expect(screen.getByText("API down")).toBeInTheDocument()
  })

  it("lag > 60s formata como minutos", async () => {
    renderPanel({ ...BASE_HEALTH, lag_seconds: 120 })
    await waitFor(() => expect(screen.getByTestId("metrics-lag")).toHaveTextContent("há 2min"))
  })

  it("lag > 3600s formata como horas", async () => {
    renderPanel({ ...BASE_HEALTH, lag_seconds: 7200 })
    await waitFor(() => expect(screen.getByTestId("metrics-lag")).toHaveTextContent("há 2h"))
  })
})
