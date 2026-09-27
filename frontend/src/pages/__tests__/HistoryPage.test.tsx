import { render, screen, fireEvent, waitFor } from "@testing-library/react"
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

// Mutável para o describe de R2-5.4 (erro num refresh não pode apagar dado
// já visível) — os demais testes usam `null` (sem erro).
let historyError: string | null = null
// Mutáveis para o describe de R3-5.3 (console.error removido — o erro já vai
// pra UI): referências ESTÁVEIS, ao contrário de um `vi.fn()` recriado a
// cada chamada de `useHistory()`, senão os testes não conseguem configurar
// `mockRejectedValueOnce` numa instância que o componente realmente usa.
const mockDownloadCSV = vi.fn()
const mockDownloadAuditCSV = vi.fn()
vi.mock("@/hooks/useHistory", () => ({
  useHistory: () => ({
    operationHistory: [],
    auditHistory: [],
    searchHistory,
    loading: false,
    error: historyError,
    fetchHistory: vi.fn(),
    fetchAuditHistory: vi.fn(),
    downloadAuditCSV: mockDownloadAuditCSV,
    downloadCSV: mockDownloadCSV,
  }),
}))

import HistoryPage from "@/pages/HistoryPage"

beforeEach(() => {
  mockAuthUser = { role: "operator", username: "op", permissions: [] }
  historyError = null
  mockDownloadCSV.mockReset().mockResolvedValue(undefined)
  mockDownloadAuditCSV.mockReset().mockResolvedValue(undefined)
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

describe("HistoryPage — R2-5.4: erro num refresh não apaga dado já visível", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("pt")
  })

  it("com dado já carregado, o erro vira um banner com retry — a tabela continua visível", () => {
    searchHistory = [makeItem({ id: 1, search_id: "srch_1", statement: "SELECT * FROM visivel" })]
    historyError = "Falha ao carregar histórico"
    render(<HistoryPage />)

    // O dado que já estava na tela continua lá.
    expect(screen.getByText("SELECT * FROM visivel")).toBeInTheDocument()
    // O erro aparece como aviso com ação de tentar de novo — não substitui a tabela.
    expect(screen.getByText("Falha ao carregar histórico")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
  })

  it("sem nenhum dado, o erro vira ErrorState de página com retry", () => {
    searchHistory = []
    historyError = "Falha ao carregar histórico"
    render(<HistoryPage />)

    expect(screen.getByRole("alert")).toHaveTextContent("Falha ao carregar histórico")
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
    expect(screen.queryByText("SELECT * FROM visivel")).not.toBeInTheDocument()
  })
})

describe("HistoryPage — R3-5.3: sem console.error (o erro já vai pra UI)", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("pt")
    // `created_at` recente: fora da janela de retenção de 7 dias o botão de
    // CSV nem aparece (ver `canDownloadStoredResult`).
    searchHistory = [makeItem({ id: 1, search_id: "srch_1", created_at: new Date().toISOString() })]
  })

  it("falha ao baixar CSV de busca não loga no console — só aparece o Notice de erro", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {})
    mockDownloadCSV.mockRejectedValueOnce(new Error("boom"))

    render(<HistoryPage />)
    const csvButton = await screen.findByRole("button", { name: "CSV" })
    fireEvent.click(csvButton)

    await waitFor(() =>
      expect(screen.getByText(/Não foi possível baixar o CSV da busca/i)).toBeInTheDocument(),
    )
    // R3-5.3: nenhum `console.error` — o Notice acima já é a UI do erro.
    expect(errorSpy).not.toHaveBeenCalled()
    errorSpy.mockRestore()
  })

  it("falha ao exportar auditoria não loga no console — só aparece o Notice de erro", async () => {
    mockAuthUser = { role: "admin", username: "admin", permissions: [] }
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {})
    mockDownloadAuditCSV.mockRejectedValueOnce(new Error("boom"))

    render(<HistoryPage />)
    fireEvent.click(await screen.findByRole("tab", { name: /Auditoria de Usuários/i }))
    fireEvent.click(await screen.findByRole("button", { name: /exportar/i }))

    await waitFor(() =>
      expect(screen.getByText(/Não foi possível exportar a auditoria em CSV/i)).toBeInTheDocument(),
    )
    expect(errorSpy).not.toHaveBeenCalled()
    errorSpy.mockRestore()
  })
})

// R4-5.4: as abas usavam `TabsTrigger` sem `TabsPanel` correspondente — o
// conteúdo de cada aba era só um `{activeTab === "x" && (...)}` solto, FORA
// do `<Tabs>`. O `aria-controls` que o `TabsTrigger` emite (`Tabs.tsx`)
// sempre apontava pra um id sem NENHUM elemento no DOM (nem antes, nem depois
// de trocar de aba) — falha de acessibilidade real: leitor de tela anuncia
// uma referência morta. Reverter o `<TabsPanel>` (voltar o conteúdo a ser um
// fragmento solto fora do `<Tabs>`) faz este teste falhar de volta, porque
// `TabsTrigger.aria-controls` só existe quando `ctx.hasPanel(value)` é
// `true` (ver `Tabs.tsx`) — sem `<TabsPanel>` nenhum se registra.
describe("HistoryPage — R4-5.4: aria-controls de cada aba resolve pra um tabpanel de verdade", () => {
  it("toda tab tem aria-controls declarado; a aba ATIVA resolve pra um role=tabpanel de verdade no DOM", async () => {
    mockAuthUser = { role: "admin", username: "admin", permissions: [] }
    await i18n.changeLanguage("pt")

    render(<HistoryPage />)

    const tabs = await screen.findAllByRole("tab")
    // 3 abas para admin: buscas, operações, auditoria.
    expect(tabs.length).toBe(3)

    // `TabsPanel` (Tabs.tsx) só mantém o painel ATIVO montado no DOM — o
    // inativo desmonta (sem `keepMounted`), então `aria-controls` de uma aba
    // inativa é "declarado" (`hasPanel`), mas o `<div role=tabpanel>` só
    // existe de verdade quando ela vira a ativa. Por isso o teste real
    // percorre as 3 abas CLICANDO em cada uma, não só olha a foto do 1º
    // render.
    for (const tab of tabs) {
      expect(tab.getAttribute("aria-controls")).toBeTruthy()
    }

    for (const tabName of [/Buscas/i, /Operações/i, /Auditoria de Usuários/i]) {
      fireEvent.click(screen.getByRole("tab", { name: tabName }))
      const activeTab = screen.getByRole("tab", { name: tabName })
      expect(activeTab).toHaveAttribute("aria-selected", "true")
      const controlsId = activeTab.getAttribute("aria-controls")
      expect(controlsId).toBeTruthy()
      // Sem o `<TabsPanel>` (achado original), este id nunca resolvia pra
      // NENHUM elemento, em NENHUM momento — nem quando a aba estava ativa.
      const panel = document.getElementById(controlsId as string)
      expect(panel).not.toBeNull()
      expect(panel).toHaveAttribute("role", "tabpanel")
      expect(panel).toHaveAttribute("aria-labelledby", activeTab.id)
      expect(panel).not.toHaveAttribute("hidden")
    }
  })
})
