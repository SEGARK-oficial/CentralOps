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
// Globais do vitest importadas explicitamente: o tsconfig do app não inclui
// `vitest/globals` em `types`, então sem isto o `tsc --noEmit` reprova o
// arquivo inteiro com "Cannot find name 'expect'".
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { MemoryRouter } from "react-router-dom"
import PipelineHealthPage from "@/pages/PipelineHealthPage"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type {
  Destination,
  DestinationHealth,
  Integration,
  IntegrationPipelineHealth,
} from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// Sem importar o bootstrap do i18n, o i18next nunca é inicializado neste
// processo e TODA asserção de texto bate contra a chave crua
// ("pipelineHealthPage.card.dataLag"). O `changeLanguage` fixa pt: o detector
// resolveria o navigator do jsdom ("en-US") e as asserções abaixo são escritas
// contra o catálogo PT, que é o que um usuário pt-BR vê.
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

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

// Cursor não temporal / sem watermark gravado ⇒ atraso dos dados NÃO É MEDÍVEL.
const healthA: IntegrationPipelineHealth = {
  integration_id: 1,
  status: "healthy",
  events_per_minute: 10,
  lag_seconds: 5,
  watermark_lag_seconds: null,
  backlog_detected: false,
  last_error: null,
  last_success_at: "2026-06-17T00:00:00Z",
  mapped_field_ratio: 0.95,
  drift_count_24h: 0,
  quarantine_count_24h: 0,
  cached_at: "2026-06-17T00:00:00Z",
}

// A forma exata do incidente de produção: a coleta terminou há 5 min, mas o dado
// que ela está processando é de 15h atrás — e o último ciclo bateu o teto.
const healthB: IntegrationPipelineHealth = {
  integration_id: 2,
  status: "degraded",
  events_per_minute: 2,
  lag_seconds: 300,
  watermark_lag_seconds: 54000,
  backlog_detected: true,
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

  // ── Atraso real vs. "quando rodou" ─────────────────────────────────────────

  it("separa 'Última coleta' de 'Atraso dos dados' com rótulos distintos", async () => {
    renderPage()
    await screen.findByText("Sophos Beta")
    // Coleta terminou há 5 min…
    expect(screen.getByTestId("health-last-collection-2")).toHaveTextContent("há 5min")
    // …e mesmo assim o dado é de 15h atrás. É esta linha que faltava.
    expect(screen.getByTestId("health-data-lag-2")).toHaveTextContent("há 15h")
    expect(screen.getByText("Atraso dos dados")).toBeInTheDocument()
  })

  it("omite o atraso dos dados quando não é medível (null), sem cair para 0", async () => {
    renderPage()
    await screen.findByText("Wazuh Alpha")
    expect(screen.getByTestId("health-last-collection-1")).toHaveTextContent("há 5s")
    expect(screen.queryByTestId("health-data-lag-1")).not.toBeInTheDocument()
    // "0s"/"há 0s" afirmaria "em dia" — jamais pode aparecer no lugar do null.
    expect(screen.getByTestId("health-last-collection-1").textContent).not.toMatch(/0s/)
  })

  it("exibe badge de backlog só na integração cujo ciclo bateu o teto", async () => {
    renderPage()
    await screen.findByText("Sophos Beta")
    expect(screen.getByTestId("health-backlog-2")).toHaveTextContent("Backlog")
    expect(screen.queryByTestId("health-backlog-1")).not.toBeInTheDocument()
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
    // fmtRate() usa 1 casa decimal abaixo de 10 — a asserção antiga ("8") nunca
    // correspondeu ao que a tela renderiza, e passava despercebida porque o
    // arquivo inteiro falhava por falta do bootstrap do i18n.
    expect(epsCell.textContent).toBe("8.0")
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
