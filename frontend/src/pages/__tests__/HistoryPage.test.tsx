import { render, screen } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
// Garante a inicialização do i18n (AuthContext, que normalmente o carrega, está mockado).
// O detector de idioma resolve pelo navigator do jsdom (en) — fixamos pt-BR para
// asserções determinísticas dos rótulos.
import i18n from "@/i18n"
import type { Client, SearchHistoryItem } from "@/types"

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { role: "operator", username: "op", permissions: [] } }),
}))

const clients: Client[] = [{ id: 1, name: "ACME Corp", is_authenticated: true }]

vi.mock("@/hooks/useClients", () => ({
  useClients: () => ({ clients, loading: false, error: null, refetch: vi.fn() }),
}))

let searchHistory: SearchHistoryItem[] = []

vi.mock("@/hooks/useHistory", () => ({
  useHistory: () => ({
    operationHistory: [],
    auditHistory: [],
    searchHistory,
    loading: false,
    error: null,
    fetchHistory: vi.fn(),
    fetchAuditHistory: vi.fn(),
    downloadAuditCSV: vi.fn(),
    downloadCSV: vi.fn(),
  }),
}))

import HistoryPage from "@/pages/HistoryPage"

function makeItem(over: Partial<SearchHistoryItem>): SearchHistoryItem {
  return {
    id: 1,
    search_id: "srch_1",
    status: "finished",
    statement: "SELECT *",
    table: "wazuh-alerts-*",
    from_ts: "2026-07-01T00:00:00Z",
    to_ts: "2026-07-02T00:00:00Z",
    result_count: 3,
    created_at: "2026-07-01T10:00:00Z",
    ...over,
  }
}

describe("HistoryPage — rótulo de cliente na aba de buscas", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("pt")
    searchHistory = [
      makeItem({ id: 1, search_id: "srch_client", client_id: 1 }),
      // Busca federada: client_id ausente (abrange vários clientes por design).
      makeItem({ id: 2, search_id: "srch_federated", client_id: undefined }),
      // Cliente que existia mas sumiu de verdade (client_id sem match na lista).
      makeItem({ id: 3, search_id: "srch_removed", client_id: 99 }),
    ]
  })

  it("mostra 'Busca federada' quando client_id é ausente (não 'Cliente removido')", () => {
    render(<HistoryPage />)
    // Mobile + desktop renderizam ambos no jsdom → getAllByText.
    expect(screen.getAllByText("Busca federada").length).toBeGreaterThan(0)
  })

  it("mostra o nome do cliente quando o client_id existe e casa", () => {
    render(<HistoryPage />)
    expect(screen.getAllByText("ACME Corp").length).toBeGreaterThan(0)
  })

  it("mostra 'Cliente removido' apenas quando havia client_id e a org sumiu", () => {
    render(<HistoryPage />)
    expect(screen.getAllByText("Cliente removido").length).toBeGreaterThan(0)
  })

  it("não rotula a busca federada como 'Cliente removido'", () => {
    // Só o item de client_id=99 deve virar 'Cliente removido'; o federado não.
    // Com 1 item removido, mobile+desktop = 2 ocorrências no máximo.
    searchHistory = [makeItem({ id: 2, search_id: "srch_federated", client_id: undefined })]
    render(<HistoryPage />)
    expect(screen.queryByText("Cliente removido")).not.toBeInTheDocument()
    expect(screen.getAllByText("Busca federada").length).toBeGreaterThan(0)
  })
})
