import { fireEvent, render, screen, waitFor } from "@testing-library/react"
// Globais do vitest importadas explicitamente: o tsconfig do app não inclui
// `vitest/globals` em `types`, então sem isto o `tsc --noEmit` reprova o
// arquivo inteiro com "Cannot find name 'expect'".
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { MemoryRouter } from "react-router-dom"
import { IntegrationHealthPanel } from "@/components/health/IntegrationHealthPanel"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { IntegrationPipelineHealth } from "@/types"

// Sem importar o bootstrap do i18n o i18next não é inicializado neste processo e
// toda asserção de texto bate contra a chave crua; `pt` porque as asserções
// abaixo são escritas contra o catálogo PT (o detector pegaria o "en-US" do jsdom).
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

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
  watermark_lag_seconds: null,
  backlog_detected: false,
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

  // ── Atraso REAL do dado (watermark) ────────────────────────────────────────

  it("watermark null: o card de atraso dos dados NÃO existe (nem como zero)", async () => {
    renderPanel({ ...BASE_HEALTH, watermark_lag_seconds: null })
    await waitFor(() => expect(screen.getByTestId("metrics-lag")).toBeInTheDocument())
    expect(screen.queryByTestId("metrics-watermark-lag")).not.toBeInTheDocument()
    expect(screen.queryByText("Atraso dos dados")).not.toBeInTheDocument()
  })

  it("o incidente: coleta terminou há 30s e o dado é de 15h atrás", async () => {
    renderPanel({ ...BASE_HEALTH, lag_seconds: 30, watermark_lag_seconds: 54000 })
    await waitFor(() => expect(screen.getByTestId("metrics-watermark-lag")).toBeInTheDocument())
    // Os dois números convivem e cada rótulo diz o que mede.
    expect(screen.getByTestId("metrics-lag")).toHaveTextContent("há 30s")
    expect(screen.getByTestId("metrics-lag")).toHaveTextContent("Última coleta")
    expect(screen.getByTestId("metrics-watermark-lag")).toHaveTextContent("há 15h")
    expect(screen.getByTestId("metrics-watermark-lag")).toHaveTextContent("Atraso dos dados")
  })

  it("backlog_detected pinta a badge no card de atraso dos dados", async () => {
    renderPanel({ ...BASE_HEALTH, watermark_lag_seconds: 54000, backlog_detected: true })
    await waitFor(() => expect(screen.getByTestId("metrics-backlog-badge")).toBeInTheDocument())
  })

  it("backlog sem watermark medível ainda aparece em card próprio", async () => {
    renderPanel({ ...BASE_HEALTH, watermark_lag_seconds: null, backlog_detected: true })
    await waitFor(() => expect(screen.getByTestId("metrics-backlog-only")).toBeInTheDocument())
    expect(screen.queryByTestId("metrics-watermark-lag")).not.toBeInTheDocument()
  })

  // ── Rolling upgrade: API antiga responde SEM os campos novos ───────────────
  // O `delete` é o ponto do teste: o tipo diz `number | null`, mas o que chega
  // pela rede é `undefined`, e um guard `!== null` deixava passar direto para
  // `Math.floor(undefined / 60)` — a tela mostrava "há NaN dias".

  it("campo ausente na resposta: nada de NaN e nenhum card de atraso dos dados", async () => {
    const legacy: Partial<IntegrationPipelineHealth> = { ...BASE_HEALTH }
    delete legacy.watermark_lag_seconds
    delete legacy.backlog_detected
    renderPanel(legacy as IntegrationPipelineHealth)

    await waitFor(() => expect(screen.getByTestId("metrics-lag")).toBeInTheDocument())
    expect(screen.queryByTestId("metrics-watermark-lag")).not.toBeInTheDocument()
    expect(screen.queryByTestId("metrics-backlog-only")).not.toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/NaN/)
  })

  it("lag_seconds ausente cai para o traço, não para 'há NaN dias'", async () => {
    const legacy: Partial<IntegrationPipelineHealth> = { ...BASE_HEALTH }
    delete legacy.lag_seconds
    renderPanel(legacy as IntegrationPipelineHealth)

    await waitFor(() => expect(screen.getByTestId("metrics-lag")).toBeInTheDocument())
    expect(screen.getByTestId("metrics-lag")).toHaveTextContent("—")
    expect(document.body.textContent).not.toMatch(/NaN/)
  })
})
