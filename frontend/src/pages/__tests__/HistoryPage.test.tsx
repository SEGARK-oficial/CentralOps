import { render, screen } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
// Garante a inicialização do i18n (AuthContext, que normalmente o carrega, está mockado).
// O detector de idioma resolve pelo navigator do jsdom (en) — fixamos pt-BR para
// asserções determinísticas dos rótulos.
import i18n from "@/i18n"
import type { Client, SearchHistoryItem } from "@/types"

// Mutável para o describe de TS-06 (user pode ser `null` — AuthContext ainda
// resolvendo a sessão): os demais testes usam o operador padrão.
let mockAuthUser: { role: string; username: string; permissions: string[] } | null = {
  role: "operator",
  username: "op",
  permissions: [],
}
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: mockAuthUser }),
}))

// PERF-03/04: jsdom não tem layout real — o virtualizer real devolveria 0
// itens sem um container com altura de verdade. Mesmo mock "materializa até
// 10" do teste de DataTable/CapturePanel; só importa acima do teto de
// virtualização (abaixo dele, o componente nem entra nesse branch).
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 10) }, (_, i) => ({
        key: i,
        index: i,
        start: i * 56,
        end: (i + 1) * 56,
      })),
    getTotalSize: () => count * 56,
    measureElement: () => {},
  }),
}))

/** Força `useMediaQuery("(min-width: 768px)")` a resolver "é desktop" — o
 *  mock global de matchMedia (test/setup.ts) sempre devolve `matches:false`. */
function mockDesktopViewport(): () => void {
  const original = window.matchMedia
  window.matchMedia = ((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
  return () => {
    window.matchMedia = original
  }
}

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

beforeEach(() => {
  mockAuthUser = { role: "operator", username: "op", permissions: [] }
})

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
    searchHistory = [makeItem({ id: 2, search_id: "srch_federated", client_id: undefined })]
    render(<HistoryPage />)
    expect(screen.queryByText("Cliente removido")).not.toBeInTheDocument()
    expect(screen.getAllByText("Busca federada").length).toBeGreaterThan(0)
  })
})

describe("HistoryPage — layout único, sem duplicar mobile+desktop (PERF-03/04)", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("pt")
  })

  it("cada busca aparece UMA única vez no DOM (antes, mobile+desktop montavam juntos)", () => {
    searchHistory = [makeItem({ id: 1, search_id: "srch_unico", statement: "SELECT * FROM unico" })]
    render(<HistoryPage />)
    expect(screen.getAllByText("SELECT * FROM unico").length).toBe(1)
  })

  it("acima do teto de virtualização, nem toda busca vai para o DOM (tabela desktop)", () => {
    const restoreViewport = mockDesktopViewport()
    try {
      searchHistory = Array.from({ length: 60 }, (_, i) =>
        makeItem({ id: i + 1, search_id: `srch_${i}`, statement: `SELECT ${i}` }),
      )
      render(<HistoryPage />)
      // O mock do virtualizer materializa só as primeiras 10 linhas.
      expect(screen.getByText("SELECT 0")).toBeInTheDocument()
      expect(screen.queryByText("SELECT 50")).not.toBeInTheDocument()
    } finally {
      restoreViewport()
    }
  })
})

describe("HistoryPage — TS-06: `user` pode ser `null`", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("pt")
    searchHistory = [makeItem({ id: 1, search_id: "srch_1" })]
  })

  it("não quebra quando o AuthContext ainda não resolveu a sessão, e some a aba de auditoria", () => {
    mockAuthUser = null
    expect(() => render(<HistoryPage />)).not.toThrow()
    expect(screen.queryByText(/Auditoria de Usuários/i)).not.toBeInTheDocument()
  })
})
