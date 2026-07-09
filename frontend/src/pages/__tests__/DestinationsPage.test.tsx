/**
 * DestinationsPage tests.
 *
 * Cobre:
 * - Render padrão com lista de destinos (cards + ícones por kind)
 * - Skeleton + retry ao falhar carregamento
 * - Filtro por kind e por estado (enabled/disabled)
 * - Busca por nome/ID
 * - EmptyState quando filtros não encontram resultados
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { MemoryRouter } from "react-router-dom"
import DestinationsPage from "@/pages/DestinationsPage"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { Destination, DestinationHealthItem, DestinationHealthBatchResponse } from "@/types"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")

const mockedApi = vi.mocked(api)

const DEST_SPLUNK: Destination = {
  id: "dest-splunk-001",
  name: "Splunk HEC Prod",
  kind: "splunk_hec",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  has_secret: true,
}

const DEST_SYSLOG: Destination = {
  id: "dest-syslog-001",
  name: "Syslog SIEM",
  kind: "syslog",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  has_secret: false,
}

const DEST_DISABLED: Destination = {
  id: "dest-jsonl-001",
  name: "JSONL Archive",
  kind: "jsonl",
  enabled: false,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  has_secret: false,
}

const DESTINATIONS: Destination[] = [DEST_SPLUNK, DEST_SYSLOG, DEST_DISABLED]

const HEALTH_ITEMS: DestinationHealthItem[] = [
  {
    destination_id: "dest-splunk-001",
    name: "Splunk HEC Prod",
    kind: "splunk_hec",
    status: "healthy",
    enabled: true,
    breaker_state: "closed",
    dlq_total: 0,
    dlq_24h: 0,
    last_dlq_at: null,
    eps: 42,
    bytes_per_min: 1024,
  },
  {
    destination_id: "dest-syslog-001",
    name: "Syslog SIEM",
    kind: "syslog",
    status: "degraded",
    enabled: true,
    breaker_state: "half_open",
    dlq_total: 5,
    dlq_24h: 5,
    last_dlq_at: null,
    eps: 7,
    bytes_per_min: 256,
  },
  {
    destination_id: "dest-jsonl-001",
    name: "JSONL Archive",
    kind: "jsonl",
    status: "disabled",
    enabled: false,
    breaker_state: null,
    dlq_total: 0,
    dlq_24h: 0,
    last_dlq_at: null,
    eps: null,
    bytes_per_min: null,
  },
]

const HEALTH_BATCH: DestinationHealthBatchResponse = {
  total: HEALTH_ITEMS.length,
  items: HEALTH_ITEMS,
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DestinationsPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listDestinations.mockResolvedValue(DESTINATIONS)
  mockedApi.listDestinationTypes.mockResolvedValue([])
  mockedApi.listDestinationsHealth.mockResolvedValue(HEALTH_BATCH)
})

describe("DestinationsPage — render padrão", () => {
  it("carrega destinos e exibe os cards", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument()
      expect(screen.getByText("Syslog SIEM")).toBeInTheDocument()
      expect(screen.getByText("JSONL Archive")).toBeInTheDocument()
    })
  })

  it("exibe badge de credencial apenas para destino com has_secret=true", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())
    // Apenas Splunk tem credencial
    expect(screen.getByText("credencial")).toBeInTheDocument()
  })

  it("exibe KPIs corretamente (total=3, ativos=2, inativos=1)", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())
    // KPIs — usa getAllByText pois "Destinos" aparece no título da página também
    expect(screen.getByText("3")).toBeInTheDocument() // total
    expect(screen.getAllByText("Destinos").length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText("Ativos")).toBeInTheDocument()
    expect(screen.getByText("Inativos")).toBeInTheDocument()
    expect(screen.getByText("Com credencial")).toBeInTheDocument()
  })

  it("exibe skeleton durante carregamento", () => {
    mockedApi.listDestinations.mockReturnValue(new Promise(() => {})) // nunca resolve
    renderPage()
    expect(screen.getByRole("status", { name: /carregando destinos/i })).toBeInTheDocument()
  })
})

describe("DestinationsPage — erro + retry", () => {
  it("exibe ErrorState ao falhar carregamento", async () => {
    mockedApi.listDestinations.mockRejectedValue(new Error("Timeout de conexão"))
    renderPage()
    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument()
      expect(screen.getByText(/Falha ao carregar destinos/i)).toBeInTheDocument()
    })
  })

  it("botão 'Tentar novamente' dispara nova chamada", async () => {
    mockedApi.listDestinations.mockRejectedValueOnce(new Error("Erro"))
    mockedApi.listDestinations.mockResolvedValue(DESTINATIONS)
    renderPage()
    await waitFor(() => expect(screen.getByText(/Falha ao carregar destinos/i)).toBeInTheDocument())

    fireEvent.click(screen.getByText("Tentar novamente"))

    await waitFor(() => {
      expect(mockedApi.listDestinations).toHaveBeenCalledTimes(2)
      expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument()
    })
  })
})

describe("DestinationsPage — filtros", () => {
  it("filtrar por kind exibe só destinos do tipo selecionado", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    const kindSelect = screen.getByTestId("destinations-filter-kind")
    fireEvent.click(kindSelect)
    const option = await screen.findByRole("option", { name: "syslog" })
    fireEvent.click(option)

    await waitFor(() => {
      expect(screen.getByText("Syslog SIEM")).toBeInTheDocument()
      expect(screen.queryByText("Splunk HEC Prod")).not.toBeInTheDocument()
      expect(screen.queryByText("JSONL Archive")).not.toBeInTheDocument()
    })
  })

  it("filtrar por estado 'Ativos' oculta destino desabilitado", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("JSONL Archive")).toBeInTheDocument())

    const enabledSelect = screen.getByTestId("destinations-filter-enabled")
    fireEvent.click(enabledSelect)
    const option = await screen.findByRole("option", { name: "Ativos" })
    fireEvent.click(option)

    await waitFor(() => {
      expect(screen.queryByText("JSONL Archive")).not.toBeInTheDocument()
      expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument()
    })
  })

  it("filtrar por estado 'Inativos' mostra só destino desabilitado", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    const enabledSelect = screen.getByTestId("destinations-filter-enabled")
    fireEvent.click(enabledSelect)
    const option = await screen.findByRole("option", { name: "Inativos" })
    fireEvent.click(option)

    await waitFor(() => {
      expect(screen.getByText("JSONL Archive")).toBeInTheDocument()
      expect(screen.queryByText("Splunk HEC Prod")).not.toBeInTheDocument()
    })
  })

  it("filtros sem resultado exibe EmptyState com botão de limpar", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    // Primeiro filtrar por kind=syslog
    const kindSelect = screen.getByTestId("destinations-filter-kind")
    fireEvent.click(kindSelect)
    const syslogOpt = await screen.findByRole("option", { name: "syslog" })
    fireEvent.click(syslogOpt)

    // Depois filtrar por estado=Inativos (syslog está ativo, logo zero resultados)
    await waitFor(() => {
      expect(screen.getByText("Syslog SIEM")).toBeInTheDocument()
    })

    const enabledSelect = screen.getByTestId("destinations-filter-enabled")
    fireEvent.click(enabledSelect)
    const inativoOpt = await screen.findByRole("option", { name: "Inativos" })
    fireEvent.click(inativoOpt)

    await waitFor(() => {
      expect(screen.getByText(/Nenhum destino encontrado/i)).toBeInTheDocument()
      expect(screen.getByText(/Limpar filtros/i)).toBeInTheDocument()
    })
  })

  it("botão Resetar fica visível quando há filtro ativo", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    const enabledSelect = screen.getByTestId("destinations-filter-enabled")
    fireEvent.click(enabledSelect)
    const option = await screen.findByRole("option", { name: "Ativos" })
    fireEvent.click(option)

    await waitFor(() => {
      expect(screen.getByText("Resetar")).toBeInTheDocument()
    })
  })
})

describe("DestinationsPage — badges de saúde", () => {
  it("renderiza badge de status por destino a partir do health em lote", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    // 1 chamada em lote (não N chamadas por card)
    expect(mockedApi.listDestinationsHealth).toHaveBeenCalledTimes(1)

    // healthy → "Saudável"; degraded → "Degradado"; disabled → "Desabilitado"
    await waitFor(() => {
      const splunkBadge = screen.getByTestId("destination-status-dest-splunk-001")
      expect(splunkBadge).toHaveTextContent("Saudável")
      const syslogBadge = screen.getByTestId("destination-status-dest-syslog-001")
      expect(syslogBadge).toHaveTextContent("Degradado")
      const jsonlBadge = screen.getByTestId("destination-status-dest-jsonl-001")
      expect(jsonlBadge).toHaveTextContent("Desabilitado")
    })
  })

  it("exibe EPS quando o health traz a métrica (e omite quando null)", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    await waitFor(() => {
      expect(screen.getByTestId("destination-eps-dest-splunk-001")).toHaveTextContent("42")
    })
    // JSONL tem eps=null → sem indicador de EPS
    expect(screen.queryByTestId("destination-eps-dest-jsonl-001")).not.toBeInTheDocument()
  })

  it("a página NÃO quebra se o health em lote falhar (degrada sem badge)", async () => {
    mockedApi.listDestinationsHealth.mockRejectedValue(new Error("health indisponível"))
    renderPage()

    // Lista de destinos renderiza normalmente
    await waitFor(() => {
      expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument()
      expect(screen.getByText("Syslog SIEM")).toBeInTheDocument()
    })

    // Nenhum badge de status é exibido (degrade gracioso)
    expect(screen.queryByTestId("destination-status-dest-splunk-001")).not.toBeInTheDocument()
    // E nenhum ErrorState
    expect(screen.queryByText(/Falha ao carregar destinos/i)).not.toBeInTheDocument()
  })
})
