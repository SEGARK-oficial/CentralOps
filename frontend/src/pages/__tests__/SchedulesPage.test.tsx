import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import i18n from "@/i18n"
import { SchedulesPage } from "@/pages/SchedulesPage"
import type { Integration, Query, Schedule, SearchHistoryItem } from "@/types"

vi.mock("@/services/api", () => ({
  listSchedules: vi.fn(),
  getScheduleHistory: vi.fn(),
  listEmails: vi.fn().mockResolvedValue([]),
  listQueries: vi.fn(),
  listIntegrations: vi.fn(),
  createSchedule: vi.fn(),
  deleteSchedule: vi.fn(),
  downloadStoredCSV: vi.fn(),
}))

import * as api from "@/services/api"

const query: Query = { id: 10, title: "Logins suspeitos", statement: "SELECT *", table: "auth", client_ids: [1, 2] }

const integrations = [
  { id: 1, name: "ACME Corp", is_authenticated: true, tenant_id: "t1", is_active: true, platform: "sophos" },
  { id: 2, name: "Globex Industries", is_authenticated: true, tenant_id: "t2", is_active: true, platform: "sophos" },
] as unknown as Integration[]

const schedule: Schedule = {
  id: 100,
  query_id: 10,
  query_title: "Logins suspeitos",
  client_ids: [1, 2],
  interval_value: 6,
  interval_unit: "hours",
  lookback_value: 1,
  lookback_unit: "days",
  notify_on_results: true,
  next_run: "2026-06-15T22:00:00Z",
  last_run_at: "2026-06-15T16:00:00Z",
}

function makeHistory(n: number): SearchHistoryItem[] {
  return Array.from({ length: n }, (_, i) => ({
    id: 1000 + i,
    search_id: `srch_${1000 + i}_opaque`,
    client_id: i % 2 === 0 ? 1 : 2,
    schedule_id: 100,
    status: i % 5 === 0 ? "failed" : "finished",
    statement: "SELECT *",
    table: "auth",
    from_ts: "2026-06-10T00:00:00Z",
    to_ts: "2026-06-10T23:59:59Z",
    result_count: i % 5 === 0 ? 0 : i,
    created_at: `2026-06-10T${String(8 + (i % 12)).padStart(2, "0")}:00:00Z`,
  }))
}

describe("SchedulesPage — histórico", () => {
  beforeEach(async () => {
    // O detector do i18n resolve pelo navigator do jsdom (en); fixamos pt-BR para as
    // asserções de rótulos localizados abaixo.
    await i18n.changeLanguage("pt")
    vi.clearAllMocks()
    ;(api.listSchedules as ReturnType<typeof vi.fn>).mockResolvedValue([schedule])
    ;(api.listQueries as ReturnType<typeof vi.fn>).mockResolvedValue([query])
    ;(api.listIntegrations as ReturnType<typeof vi.fn>).mockResolvedValue(integrations)
    ;(api.getScheduleHistory as ReturnType<typeof vi.fn>).mockResolvedValue(makeHistory(23))
  })

  async function openHistory() {
    render(<SchedulesPage />)
    await waitFor(() => expect(screen.getAllByText("Logins suspeitos").length).toBeGreaterThan(0))
    fireEvent.click(screen.getAllByRole("button", { name: /Histórico/i })[0])
    await waitFor(() =>
      expect(screen.getByRole("table", { name: /Histórico do agendamento/i })).toBeInTheDocument(),
    )
    return screen.getByRole("table", { name: /Histórico do agendamento/i })
  }

  it("mostra coluna Query / Ambiente em vez de Search ID", async () => {
    const table = await openHistory()
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent?.trim())
    expect(headers).toContain("Query / Ambiente")
    expect(headers).not.toContain("Search ID")
  })

  it("exibe o nome do ambiente (cliente) que executou cada item", async () => {
    const table = await openHistory()
    // Os nomes dos ambientes vêm do mapeamento client_id -> integração.
    expect(within(table).getAllByText("ACME Corp").length).toBeGreaterThan(0)
    expect(within(table).getAllByText("Globex Industries").length).toBeGreaterThan(0)
    // O search_id opaco não deve mais aparecer na tabela.
    expect(within(table).queryByText(/srch_1000_opaque/)).not.toBeInTheDocument()
  })

  it("pagina o histórico (8 por página) em vez de listar tudo", async () => {
    const table = await openHistory()
    expect(within(table).getAllByRole("row").length).toBe(1 + 8) // header + 8
    expect(screen.getByText(/Mostrando/i)).toHaveTextContent("1–8")
    expect(screen.getByText(/Página 1 de 3/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole("button", { name: /Próxima/i }))
    await waitFor(() => expect(screen.getByText(/Página 2 de 3/i)).toBeInTheDocument())
  })

  it("filtra por status (Falhas)", async () => {
    await openHistory()
    fireEvent.click(screen.getByRole("button", { name: /^Falhas$/i }))
    await waitFor(() => {
      const table = screen.getByRole("table", { name: /Histórico do agendamento/i })
      const statusCells = within(table)
        .getAllByRole("row")
        .slice(1)
        .map((r) => r.querySelector("td:nth-child(2)")?.textContent?.trim())
      expect(statusCells.every((s) => s === "Falhou")).toBe(true)
    })
  })

  it("oferece download de CSV na tela de visualizar o resultado", async () => {
    const item: SearchHistoryItem = {
      id: 2000,
      search_id: "srch_view_csv",
      client_id: 1,
      schedule_id: 100,
      status: "finished",
      statement: "SELECT *",
      table: "auth",
      from_ts: "2026-06-10T00:00:00Z",
      to_ts: "2026-06-10T23:59:59Z",
      result_count: 5,
      created_at: "2026-06-10T08:00:00Z",
    }
    ;(api.getScheduleHistory as ReturnType<typeof vi.fn>).mockResolvedValue([item])
    const table = await openHistory()

    // Abre o modal de visualização (botão "Ver" da linha do histórico).
    fireEvent.click(within(table).getByRole("button", { name: /^Ver$/i }))

    // O modal expõe o download do CSV (reusa /history/result/{id}/csv).
    const csvButton = await screen.findByRole("button", { name: /Baixar CSV/i })
    expect(csvButton).toBeEnabled()
    fireEvent.click(csvButton)
    expect(api.downloadStoredCSV).toHaveBeenCalledWith("srch_view_csv")
  })

  // R3-9.1: `refreshSchedules`/`refreshNotificationRecipients` viraram
  // `useCallback([t])` pra satisfazer react-hooks/exhaustive-deps no efeito de
  // carga inicial. Prova que o memo não introduziu refetch num re-render sem
  // troca de idioma.
  it("re-render do componente sem trocar de idioma NÃO rechama a carga inicial", async () => {
    const { rerender } = render(<SchedulesPage />)
    await waitFor(() => expect(screen.getAllByText("Logins suspeitos").length).toBeGreaterThan(0))
    expect(api.listSchedules).toHaveBeenCalledTimes(1)
    expect(api.listEmails).toHaveBeenCalledTimes(1)

    rerender(<SchedulesPage />)

    await waitFor(() => expect(screen.getAllByText("Logins suspeitos").length).toBeGreaterThan(0))
    expect(api.listSchedules).toHaveBeenCalledTimes(1)
    expect(api.listEmails).toHaveBeenCalledTimes(1)
  })
})

// Pilar 4: carregamento inicial da lista de agendamentos e do histórico.
describe("SchedulesPage — Pilar 4 (ErrorState com retry)", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("pt")
    vi.clearAllMocks()
    ;(api.listQueries as ReturnType<typeof vi.fn>).mockResolvedValue([query])
    ;(api.listIntegrations as ReturnType<typeof vi.fn>).mockResolvedValue(integrations)
  })

  it("listSchedules rejeitando no load inicial mostra ErrorState, não EmptyState", async () => {
    ;(api.listSchedules as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("timeout"))
    render(<SchedulesPage />)

    await waitFor(() => expect(screen.getByText("timeout")).toBeInTheDocument())
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
    expect(screen.queryByText("Nenhum agendamento cadastrado")).not.toBeInTheDocument()
  })

  it("Tentar novamente recarrega a lista de agendamentos", async () => {
    ;(api.listSchedules as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("timeout"))
    render(<SchedulesPage />)
    await screen.findByText("timeout")

    ;(api.listSchedules as ReturnType<typeof vi.fn>).mockResolvedValueOnce([schedule])
    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    await waitFor(() => expect(screen.getAllByText("Logins suspeitos").length).toBeGreaterThan(0))
    expect(screen.queryByText("timeout")).not.toBeInTheDocument()
  })

  it("getScheduleHistory rejeitando mostra ErrorState (não empilha com o EmptyState de histórico)", async () => {
    ;(api.listSchedules as ReturnType<typeof vi.fn>).mockResolvedValue([schedule])
    ;(api.getScheduleHistory as ReturnType<typeof vi.fn>).mockRejectedValue(new Error("falha ao buscar histórico"))

    render(<SchedulesPage />)
    await waitFor(() => expect(screen.getAllByText("Logins suspeitos").length).toBeGreaterThan(0))
    fireEvent.click(screen.getAllByRole("button", { name: /Histórico/i })[0])

    await waitFor(() => expect(screen.getByText("falha ao buscar histórico")).toBeInTheDocument())
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
    expect(screen.queryByText(/nenhuma execução/i)).not.toBeInTheDocument()
  })
})

// R3-8.4: `useForm` já mostrava o erro por campo (`error=` no Select/Input),
// mas o foco nunca ia atrás — e o `Select` nem aceitava `ref` até esta
// rodada. Sem os dois, o operador lia "Selecione uma query cadastrada" no
// topo do campo e tinha que clicar nele manualmente.
describe("SchedulesPage — foco no campo inválido (R3-8.4)", () => {
  // Query SEM `client_ids` de propósito: o form auto-seleciona a única query
  // disponível (e os clientes DELA, se ela declarar algum) assim que carrega
  // — com `client_ids: []` na query, esse auto-preenchimento fica só no
  // `query_id`, e "Clientes" (Select `multiple`) permanece vazio/inválido.
  // É o cenário real que expõe o bug: o `Select` só passou a aceitar `ref`
  // nesta rodada, e é exatamente o campo `multiple` que a Rodada 2 quebrou.
  const queryNoDefaultClients: Query = { ...query, client_ids: [] }

  beforeEach(async () => {
    await i18n.changeLanguage("pt")
    vi.clearAllMocks()
    ;(api.listQueries as ReturnType<typeof vi.fn>).mockResolvedValue([queryNoDefaultClients])
    ;(api.listIntegrations as ReturnType<typeof vi.fn>).mockResolvedValue(integrations)
    ;(api.listSchedules as ReturnType<typeof vi.fn>).mockResolvedValue([])
  })

  it("submeter sem clientes selecionados foca o Select de Clientes (1º campo realmente inválido)", async () => {
    render(<SchedulesPage />)
    await screen.findByText("Novo agendamento")
    // Aguarda queries/clientes carregarem — o botão fica desabilitado
    // (`formBusy`) enquanto `queriesLoading`/`clientsLoading`.
    await waitFor(() => expect(screen.getByRole("button", { name: "Criar agendamento" })).not.toBeDisabled())
    // A query única já vem auto-selecionada (efeito da própria página) — o
    // campo que fica inválido de verdade é "Clientes".
    await screen.findByText("Logins suspeitos")

    fireEvent.click(screen.getByRole("button", { name: "Criar agendamento" }))

    await screen.findByText("Selecione ao menos um cliente")
    expect(document.activeElement).toHaveAccessibleName("Clientes")
  })
})
