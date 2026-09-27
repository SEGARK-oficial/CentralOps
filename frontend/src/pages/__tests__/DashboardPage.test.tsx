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
      expect(mockedApi.getDashboardSummary).toHaveBeenCalledWith(
        {
          organization_id: 10,
          integration_id: null,
          platform: "wazuh",
          days: 7,
        },
        // R2-6.3: 2º argumento agora carrega o AbortSignal do request em voo.
        { signal: expect.any(AbortSignal) },
      )
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
    // O enum do backend chega como "degraded"; a UI em português mostra o rótulo.
    expect(screen.getByText("Degradado")).toBeInTheDocument()
    expect(screen.queryByText("degraded")).toBeNull()
    expect(screen.getByText(/timeout/)).toBeInTheDocument()
    // by_platform
    expect(screen.getByText("Integrações por plataforma")).toBeInTheDocument()
    expect(screen.getByText("wazuh")).toBeInTheDocument()

    // clicar num item degradado seleciona a integração (deep-link p/ detalhe)
    fireEvent.click(screen.getByText("Wazuh Prod"))
    expect(setSelectedIntegrationId).toHaveBeenCalledWith(100)
  })

  it("mostra as contagens de escopo e o horário de geração na barra de escopo", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())
    mockedApi.getDashboardSummary.mockResolvedValue(buildSummary())

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    // A barra troca o parágrafo explicativo por rótulo + número: "N clientes ·
    // N integrações · N ativas", e a geração vira "hora · relativo".
    expect(await screen.findByText("1 clientes · 2 integrações · 2 ativas")).toBeInTheDocument()
    expect(screen.getByText(/·\s*\d+\s*(min|h|d) atrás|·\s*agora/)).toBeInTheDocument()
  })
})

describe("DashboardPage — R2-6.3 (erro não apaga dado visível + retry)", () => {
  it("erro no LOAD INICIAL (sem summary ainda) mostra ErrorState com retry, preservando o header", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())
    mockedApi.getDashboardSummary.mockRejectedValueOnce(new Error("Falha de rede"))

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByRole("alert")).toHaveTextContent("Falha de rede")
    // Header (título + botão Atualizar) continua visível mesmo em erro.
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()

    mockedApi.getDashboardSummary.mockResolvedValueOnce(buildSummary())
    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    expect(await screen.findByText("Ingestão (EPS)")).toBeInTheDocument()
  })

  it("erro num REFRESH (summary já carregado) mantém o dashboard visível e mostra aviso com retry", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())
    mockedApi.getDashboardSummary.mockResolvedValueOnce(buildSummary())

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Ingestão (EPS)")).toBeInTheDocument()

    mockedApi.getDashboardSummary.mockRejectedValueOnce(new Error("timeout no refresh"))
    fireEvent.click(screen.getByRole("button", { name: /^atualizar$/i }))

    await waitFor(() => {
      expect(screen.getByText("timeout no refresh")).toBeInTheDocument()
    })
    // O dashboard NÃO some — o KPI que já estava na tela continua lá.
    expect(screen.getByText("Ingestão (EPS)")).toBeInTheDocument()
    expect(screen.getByText("Top fontes por volume")).toBeInTheDocument()
  })

  it("uma resposta atrasada de um filtro ANTERIOR não sobrescreve o summary do filtro atual", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())

    let resolveFirst!: (v: DashboardSummaryV2) => void
    const firstCall = new Promise<DashboardSummaryV2>((resolve) => {
      resolveFirst = resolve
    })
    mockedApi.getDashboardSummary.mockReturnValueOnce(firstCall)

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    // 2ª chamada (refresh) resolve ANTES da 1ª (que ainda está pendurada).
    mockedApi.getDashboardSummary.mockResolvedValueOnce(
      buildSummary({ top_buckets: [] , kpis: [{ id: "quarantine_rate", label: "Quarentena 24h", value: "9.9%", sub: "taxa 24h", icon_id: "shield-alert", severity: "ok" }] }),
    )
    fireEvent.click(await screen.findByRole("button", { name: /^atualizar$/i }))
    expect(await screen.findByText("Quarentena 24h")).toBeInTheDocument()

    // A 1ª chamada (do filtro/estado anterior) finalmente resolve — não deve
    // reverter a tela pro dado antigo.
    resolveFirst(buildSummary())
    await new Promise((r) => setTimeout(r, 0))

    expect(screen.getByText("Quarentena 24h")).toBeInTheDocument()
    expect(screen.queryByText("Ingestão (EPS)")).not.toBeInTheDocument()
  })

  it("skeleton (role=status) aparece durante o load inicial, no lugar do LoadingSpinner de página inteira", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())
    let resolveSummary!: (v: DashboardSummaryV2) => void
    mockedApi.getDashboardSummary.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSummary = resolve
      }),
    )

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(screen.getByRole("status")).toBeInTheDocument()
    // Header já está visível durante o skeleton (não é substituído por ele).
    expect(screen.getByRole("heading", { name: "Dashboard" })).toBeInTheDocument()

    resolveSummary(buildSummary())
    await screen.findByText("Ingestão (EPS)")
  })
})

describe("DashboardPage — R3-6.1 (indicador de ocupado ao trocar filtro com summary já carregado)", () => {
  it("mostra aria-busy + status sr-only ao trocar a janela de tempo, e some quando o novo summary chega", async () => {
    mockedUsePlatform.mockReturnValue(mockPlatformContext())
    mockedApi.getDashboardSummary.mockResolvedValueOnce(buildSummary())

    render(
      <MemoryRouter>
        <DashboardPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText("Ingestão (EPS)")).toBeInTheDocument()

    // Troca de `days` (7d → 30d) dispara um novo `loadSummary()` com o
    // summary ANTERIOR ainda montado — sem o fix, nada na tela indicava a
    // busca em curso além dos KPIs antigos parados.
    let resolveSecond!: (v: DashboardSummaryV2) => void
    mockedApi.getDashboardSummary.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveSecond = resolve
      }),
    )

    fireEvent.click(screen.getByLabelText("Janela"))
    fireEvent.click(screen.getByRole("option", { name: "30 dias" }))

    const status = await screen.findByRole("status", { name: "" })
    expect(status).toHaveTextContent(/atualizando/i)

    const busyContainer = document.querySelector('[aria-busy="true"]')
    expect(busyContainer).not.toBeNull()

    // O KPI antigo continua visível durante a busca — não é substituído por
    // um estado de loading de página inteira.
    expect(screen.getByText("Ingestão (EPS)")).toBeInTheDocument()

    resolveSecond(buildSummary({ kpis: [{ id: "ingest_eps", label: "Ingestão (EPS)", value: 99, sub: "eventos/s", icon_id: "activity", severity: "ok" }] }))

    await waitFor(() => {
      expect(document.querySelector('[aria-busy="true"]')).toBeNull()
    })
    // A região `role=status` permanece montada (live region estável), mas
    // esvazia — não fica ecoando "Atualizando..." depois que a busca termina.
    expect(status).toHaveTextContent("")
  })
})
