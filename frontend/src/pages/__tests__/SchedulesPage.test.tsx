import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
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
  beforeEach(() => {
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
})
