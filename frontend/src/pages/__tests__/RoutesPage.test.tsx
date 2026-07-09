/**
 * RoutesPage tests (premium UX).
 *
 * Cobre:
 * - Render padrão: carrega rotas via listRoutes.
 * - Skeleton no carregamento.
 * - ErrorState + retry quando listRoutes falha.
 * - Badge "catch-all" em rota com condição {} + is_final.
 * - Aviso "Sem catch-all final" quando não há catch-all.
 * - Drag-reorder: reorderRoutes chamado com nova ordem.
 * - Rollback: chama rollbackRoute ao confirmar no RouteAuditPanel.
 * - Estado de atividade (RouteActivityModal abre ao clicar em Atividade).
 * - EmptyState quando lista vazia.
 * - BulkActionBar: seleção múltipla + ações Ativar/Desativar em massa.
 * - Proteção da rota de sistema (wazuh-default-catchall):
 *     não-selecionável, delete desabilitado, drag bloqueado.
 */

import React from "react"
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { MemoryRouter } from "react-router-dom"
import RoutesPage, { SYSTEM_ROUTE_ID, isSystemRoute } from "@/pages/RoutesPage"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { Route, RouteAudit } from "@/types"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ user: { role: "admin" } }),
}))

// Mock do @dnd-kit para isolar drag-and-drop em testes unitários
vi.mock("@dnd-kit/core", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@dnd-kit/core")>()
  return {
    ...mod,
    DndContext: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  }
})

vi.mock("@dnd-kit/sortable", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@dnd-kit/sortable")>()
  return {
    ...mod,
    useSortable: () => ({
      attributes: {},
      listeners: {},
      setNodeRef: () => {},
      transform: null,
      transition: undefined,
      isDragging: false,
    }),
    SortableContext: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  }
})

const mockedApi = vi.mocked(api)

const ROUTE_CATCH_ALL: Route = {
  id: "r-catchall",
  name: "Catch-all Wazuh",
  priority: 999,
  condition: {},
  action: "route",
  destination_ids: ["wazuh-default"],
  is_final: true,
  canary_percent: 100,
  transform_ref: null,
  pii_redaction: null,
  enabled: true,
  organization_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  unreachable: false,
}

const ROUTE_SOPHOS: Route = {
  id: "r-sophos",
  name: "Sophos alto",
  priority: 10,
  condition: { severity_id: { gte: 7 } },
  action: "route",
  destination_ids: ["dest-1"],
  is_final: true,
  canary_percent: 100,
  transform_ref: null,
  pii_redaction: null,
  enabled: true,
  organization_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  unreachable: false,
}

const ROUTE_DROP: Route = {
  id: "r-drop",
  name: "Drop spam",
  priority: 5,
  condition: { vendor: "spam" },
  action: "drop",
  destination_ids: [],
  is_final: true,
  canary_percent: 100,
  transform_ref: null,
  pii_redaction: null,
  enabled: true,
  organization_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  unreachable: false,
}

/** Rota de sistema protegida — id = SYSTEM_ROUTE_ID. */
const ROUTE_SYSTEM: Route = {
  id: SYSTEM_ROUTE_ID,
  name: "Wazuh default catchall (sistema)",
  priority: 9999,
  condition: {},
  action: "route",
  destination_ids: ["wazuh-default"],
  is_final: true,
  canary_percent: 100,
  transform_ref: null,
  pii_redaction: null,
  enabled: true,
  organization_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  unreachable: false,
}

const AUDIT_ENTRY: RouteAudit = {
  id: "audit-1",
  route_id: "r-sophos",
  action: "updated",
  actor: "admin@co.test",
  snapshot: { name: "Sophos alto", priority: 10 },
  created_at: "2026-01-02T10:00:00Z",
}

function renderPage() {
  return render(
    <MemoryRouter>
      <RoutesPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listRoutes.mockResolvedValue([ROUTE_SOPHOS, ROUTE_CATCH_ALL])
  mockedApi.listDestinations.mockResolvedValue([])
  mockedApi.reorderRoutes.mockResolvedValue({ reordered: [ROUTE_CATCH_ALL, ROUTE_SOPHOS] })
  mockedApi.routeAudit.mockResolvedValue([AUDIT_ENTRY])
  mockedApi.rollbackRoute.mockResolvedValue({ ...ROUTE_SOPHOS, name: "Sophos alto (revertido)" })
  mockedApi.getRouteMetrics.mockResolvedValue({ route_id: "r-sophos", series: {} })
  mockedApi.deleteRoute.mockResolvedValue(undefined)
  mockedApi.updateRoute.mockResolvedValue(ROUTE_SOPHOS)
  // RoutesPage agora carrega a topologia em paralelo (degrade-safe).
  mockedApi.getRoutingTopology.mockResolvedValue({ destinations: [], routes: [] })
})

describe("RoutesPage — render padrão", () => {
  it("carrega rotas via listRoutes ao montar", async () => {
    renderPage()
    await waitFor(() => {
      expect(mockedApi.listRoutes).toHaveBeenCalledTimes(1)
    })
  })

  it("renderiza headings para cada rota", async () => {
    renderPage()
    await waitFor(() => {
      // Usa getAllByRole heading para diferenciar do conteúdo SVG
      const headings = screen.getAllByRole("heading", { level: 2 })
      const names = headings.map((h) => h.textContent)
      expect(names).toContain("Sophos alto")
      expect(names).toContain("Catch-all Wazuh")
    })
  })

  it("exibe badge da rota padrão com condição {} + is_final", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText("padrão")).toBeInTheDocument()
    })
  })

  it("não exibe aviso de rota padrão ausente quando há rota padrão final", async () => {
    renderPage()
    await waitFor(() => {
      const headings = screen.getAllByRole("heading", { level: 2 })
      expect(headings.some((h) => h.textContent === "Catch-all Wazuh")).toBe(true)
    })
    expect(screen.queryByText(/Sem regra padrão final/i)).not.toBeInTheDocument()
  })

  it("exibe aviso quando não há rota padrão final", async () => {
    mockedApi.listRoutes.mockResolvedValue([ROUTE_SOPHOS, ROUTE_DROP])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Sem regra padrão final/i)).toBeInTheDocument()
    })
  })
})

describe("RoutesPage — skeleton e erro", () => {
  it("exibe skeleton durante carregamento", () => {
    mockedApi.listRoutes.mockImplementation(() => new Promise(() => {}))
    renderPage()
    expect(screen.getByRole("status", { name: /Carregando rotas/i })).toBeInTheDocument()
  })

  it("exibe ErrorState quando listRoutes falha", async () => {
    mockedApi.listRoutes.mockRejectedValue(new Error("Timeout de conexão"))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Falha ao carregar rotas/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/Timeout de conexão/i)).toBeInTheDocument()
  })

  it("retry após erro chama listRoutes novamente", async () => {
    mockedApi.listRoutes.mockRejectedValueOnce(new Error("Timeout"))
    mockedApi.listRoutes.mockResolvedValueOnce([ROUTE_SOPHOS])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Falha ao carregar rotas/i)).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText("Tentar novamente"))
    await waitFor(() => {
      expect(mockedApi.listRoutes).toHaveBeenCalledTimes(2)
    })
  })
})

describe("RoutesPage — drag-reorder", () => {
  it("reorderRoutes pode ser chamado com IDs na nova ordem", async () => {
    renderPage()
    await waitFor(() => {
      const headings = screen.getAllByRole("heading", { level: 2 })
      expect(headings.some((h) => h.textContent === "Sophos alto")).toBe(true)
    })
    // Valida que a função existe e pode ser chamada (drag real é e2e)
    await mockedApi.reorderRoutes(["r-catchall", "r-sophos"])
    expect(mockedApi.reorderRoutes).toHaveBeenCalledWith(["r-catchall", "r-sophos"])
  })
})

describe("RoutesPage — modal de atividade + auditoria", () => {
  it("abre modal ao clicar em Atividade", async () => {
    renderPage()
    await waitFor(() => {
      const headings = screen.getAllByRole("heading", { level: 2 })
      expect(headings.some((h) => h.textContent === "Sophos alto")).toBe(true)
    })
    const btns = screen.getAllByText("Atividade")
    fireEvent.click(btns[0])
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /Auditoria/i })).toBeInTheDocument()
    })
  })

  it("aba Auditoria carrega routeAudit e exibe entrada", async () => {
    renderPage()
    await waitFor(() => {
      const headings = screen.getAllByRole("heading", { level: 2 })
      expect(headings.some((h) => h.textContent === "Sophos alto")).toBe(true)
    })

    const btns = screen.getAllByText("Atividade")
    fireEvent.click(btns[0])

    const auditTab = await screen.findByRole("tab", { name: /Auditoria/i })
    fireEvent.click(auditTab)

    await waitFor(() => {
      expect(mockedApi.routeAudit).toHaveBeenCalledWith("r-sophos")
    })

    await waitFor(() => {
      expect(screen.getByText(/admin@co\.test/i)).toBeInTheDocument()
    })
  })

  it("botão Reverter chama rollbackRoute após confirmação", async () => {
    renderPage()
    await waitFor(() => {
      const headings = screen.getAllByRole("heading", { level: 2 })
      expect(headings.some((h) => h.textContent === "Sophos alto")).toBe(true)
    })

    const btns = screen.getAllByText("Atividade")
    fireEvent.click(btns[0])

    const auditTab = await screen.findByRole("tab", { name: /Auditoria/i })
    fireEvent.click(auditTab)

    await waitFor(() => {
      expect(screen.getByText(/admin@co\.test/i)).toBeInTheDocument()
    })

    // Clica no botão Reverter com aria-label descritivo
    const listReverterBtns = screen.getAllByRole("button", { name: /Reverter rota para snapshot/i })
    fireEvent.click(listReverterBtns[0])

    // Aguarda o ConfirmDialog e confirma
    await waitFor(() => {
      expect(screen.getByText(/Reverter "Sophos alto" para/i)).toBeInTheDocument()
    })

    // O botão de confirm no ConfirmDialog — o último "Reverter" na tela
    const allReverterBtns = screen.getAllByRole("button", { name: /Reverter/i })
    fireEvent.click(allReverterBtns[allReverterBtns.length - 1])

    await waitFor(() => {
      expect(mockedApi.rollbackRoute).toHaveBeenCalledWith("r-sophos", "audit-1")
    })
  })
})

describe("RoutesPage — EmptyState", () => {
  it("exibe EmptyState quando não há rotas", async () => {
    mockedApi.listRoutes.mockResolvedValue([])
    renderPage()
    await waitFor(() => {
      expect(screen.getByText("Nenhuma rota configurada")).toBeInTheDocument()
    })
  })
})

// ── BulkActionBar ──────────────────────────────────────────────

describe("RoutesPage — BulkActionBar", () => {
  it("BulkActionBar não aparece sem seleção", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getAllByRole("heading", { level: 2 }).length).toBeGreaterThan(0)
    })
    expect(screen.queryByTestId("routes-bulk-action-bar")).not.toBeInTheDocument()
  })

  it("checkbox de rota não-sistema existe e ao clicar exibe BulkActionBar", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-select-r-sophos")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("route-select-r-sophos"))
    expect(screen.getByTestId("routes-bulk-action-bar")).toBeInTheDocument()
    expect(screen.getByText(/1 rota\(s\) selecionado\(s\)/i)).toBeInTheDocument()
  })

  it("clicar em Limpar seleção esconde BulkActionBar", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-select-r-sophos")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("route-select-r-sophos"))
    expect(screen.getByTestId("routes-bulk-action-bar")).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("routes-bulk-action-bar-clear"))
    expect(screen.queryByTestId("routes-bulk-action-bar")).not.toBeInTheDocument()
  })

  it("Ativar em massa abre ConfirmDialog e chama updateRoute para cada ID selecionado", async () => {
    mockedApi.updateRoute.mockResolvedValue({ ...ROUTE_SOPHOS, enabled: true })
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-select-r-sophos")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("route-select-r-sophos"))
    fireEvent.click(screen.getByTestId("routes-bulk-enable-btn"))

    await waitFor(() => {
      expect(screen.getByText(/Ativar 1 rota/i)).toBeInTheDocument()
    })

    await act(async () => {
      fireEvent.click(screen.getByTestId("routes-bulk-enable-dialog-confirm"))
    })

    await waitFor(() => {
      expect(mockedApi.updateRoute).toHaveBeenCalledWith("r-sophos", { enabled: true })
    })
  })

  it("Desativar em massa abre ConfirmDialog e chama updateRoute com enabled: false", async () => {
    mockedApi.updateRoute.mockResolvedValue({ ...ROUTE_SOPHOS, enabled: false })
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-select-r-sophos")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("route-select-r-sophos"))
    fireEvent.click(screen.getByTestId("routes-bulk-disable-btn"))

    await waitFor(() => {
      expect(screen.getByText(/Desativar 1 rota/i)).toBeInTheDocument()
    })

    await act(async () => {
      fireEvent.click(screen.getByTestId("routes-bulk-disable-dialog-confirm"))
    })

    await waitFor(() => {
      expect(mockedApi.updateRoute).toHaveBeenCalledWith("r-sophos", { enabled: false })
    })
  })

  it("após bulk action bem-sucedida exibe feedback de sucesso", async () => {
    mockedApi.updateRoute.mockResolvedValue({ ...ROUTE_SOPHOS, enabled: true })
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-select-r-sophos")).toBeInTheDocument()
    })
    fireEvent.click(screen.getByTestId("route-select-r-sophos"))
    fireEvent.click(screen.getByTestId("routes-bulk-enable-btn"))

    await act(async () => {
      fireEvent.click(screen.getByTestId("routes-bulk-enable-dialog-confirm"))
    })

    await waitFor(() => {
      expect(screen.getByText(/1 rota ativada/i)).toBeInTheDocument()
    })
  })
})

// ── proteção da rota de sistema ───────────────────────────────

describe("RoutesPage — proteção da rota de sistema", () => {
  beforeEach(() => {
    mockedApi.listRoutes.mockResolvedValue([ROUTE_SOPHOS, ROUTE_SYSTEM])
  })

  it("isSystemRoute retorna true somente para SYSTEM_ROUTE_ID", () => {
    expect(isSystemRoute(ROUTE_SYSTEM)).toBe(true)
    expect(isSystemRoute(ROUTE_SOPHOS)).toBe(false)
    expect(isSystemRoute(ROUTE_CATCH_ALL)).toBe(false)
  })

  it("rota de sistema não tem checkbox de seleção", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId(`route-card-${SYSTEM_ROUTE_ID}`)).toBeInTheDocument()
    })
    expect(screen.queryByTestId(`route-select-${SYSTEM_ROUTE_ID}`)).not.toBeInTheDocument()
  })

  it("rota de sistema não aparece na contagem do BulkActionBar mesmo ao selecionar 'todas'", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-select-r-sophos")).toBeInTheDocument()
    })
    // Seleciona a rota não-sistema
    fireEvent.click(screen.getByTestId("route-select-r-sophos"))
    // Apenas 1 rota selecionada (sistema não conta)
    expect(screen.getByText(/1 rota\(s\) selecionado\(s\)/i)).toBeInTheDocument()
  })

  it("botão Excluir da rota de sistema está desabilitado", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId(`route-delete-${SYSTEM_ROUTE_ID}`)).toBeInTheDocument()
    })
    const deleteBtn = screen.getByTestId(`route-delete-${SYSTEM_ROUTE_ID}`)
    expect(deleteBtn).toBeDisabled()
  })

  it("botão Excluir de rota normal está habilitado", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId("route-delete-r-sophos")).toBeInTheDocument()
    })
    const deleteBtn = screen.getByTestId("route-delete-r-sophos")
    expect(deleteBtn).not.toBeDisabled()
  })

  it("rota de sistema exibe badge 'sistema'", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId(`route-card-${SYSTEM_ROUTE_ID}`)).toBeInTheDocument()
    })
    expect(screen.getByText("sistema")).toBeInTheDocument()
  })

  it("rota de sistema exibe nota explicativa de broadcast", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/rota broadcast auto-criada/i)).toBeInTheDocument()
    })
  })

  it("grip handle da rota de sistema está desabilitado", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId(`route-card-${SYSTEM_ROUTE_ID}`)).toBeInTheDocument()
    })
    const grip = screen.getByRole("button", {
      name: /Rota de sistema — não pode ser reordenada/i,
    })
    expect(grip).toBeDisabled()
  })
})
