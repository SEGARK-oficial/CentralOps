/**
 * RouteAuditPanel tests.
 *
 * Cobre:
 * - Render padrão: carrega routeAudit(id) e exibe entradas.
 * - Estado de carregamento: skeleton.
 * - Estado de erro: ErrorState com retry.
 * - Lista vazia.
 * - Badges de ação traduzidos (created/updated/deleted).
 * - Botão "Reverter": abre ConfirmDialog.
 * - Confirmar rollback: chama rollbackRoute(id, auditId).
 * - rollbackRoute falha: exibe Notice de erro.
 * - onRolledBack callback após sucesso.
 * - A11y: região com aria-label; botão com aria-label descritivo.
 * - Entrada deleted não tem botão Reverter.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { RouteAuditPanel } from "../RouteAuditPanel"
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

const mockedApi = vi.mocked(api)

const AUDIT_1: RouteAudit = {
  id: "audit-001",
  route_id: "r-1",
  action: "updated",
  actor: "admin@co.test",
  snapshot: { name: "Rota A", priority: 10 },
  created_at: "2026-01-10T15:30:00Z",
}

const AUDIT_2: RouteAudit = {
  id: "audit-002",
  route_id: "r-1",
  action: "created",
  actor: "devops@co.test",
  snapshot: { name: "Rota A", priority: 50 },
  created_at: "2026-01-01T10:00:00Z",
}

const ROLLED_BACK_ROUTE: Route = {
  id: "r-1",
  name: "Rota A",
  priority: 50,
  condition: {},
  action: "route",
  destination_ids: [],
  is_final: true,
  canary_percent: 100,
  transform_ref: null,
  pii_redaction: null,
  enabled: true,
  organization_id: null,
  created_at: "2026-01-01T10:00:00Z",
  updated_at: "2026-01-10T15:30:00Z",
  unreachable: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.routeAudit.mockResolvedValue([AUDIT_1, AUDIT_2])
  mockedApi.rollbackRoute.mockResolvedValue(ROLLED_BACK_ROUTE)
})

describe("RouteAuditPanel — render padrão", () => {
  it("renderiza título e chama routeAudit(id)", async () => {
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(mockedApi.routeAudit).toHaveBeenCalledWith("r-1")
    })
    expect(screen.getByText("Trilha de auditoria")).toBeInTheDocument()
  })

  it("exibe entradas de auditoria após carregamento", async () => {
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText("admin@co.test")).toBeInTheDocument()
      expect(screen.getByText("devops@co.test")).toBeInTheDocument()
    })
  })

  it("exibe badge de ação traduzida", async () => {
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText("atualizada")).toBeInTheDocument()
      expect(screen.getByText("criada")).toBeInTheDocument()
    })
  })

  it("tem region com aria-label da rota", () => {
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    expect(screen.getByRole("region", { name: /Auditoria da rota Rota A/i })).toBeInTheDocument()
  })
})

describe("RouteAuditPanel — estado de carregamento", () => {
  it("exibe role=status durante carregamento", () => {
    mockedApi.routeAudit.mockImplementation(() => new Promise(() => {}))
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    expect(screen.getByRole("status", { name: /Carregando trilha de auditoria/i })).toBeInTheDocument()
  })
})

describe("RouteAuditPanel — estado de erro", () => {
  it("exibe ErrorState quando routeAudit falha", async () => {
    mockedApi.routeAudit.mockRejectedValue(new Error("API indisponível"))
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText(/Falha ao carregar auditoria/i)).toBeInTheDocument()
    })
    expect(screen.getByText("API indisponível")).toBeInTheDocument()
  })

  it("retry chama routeAudit novamente", async () => {
    mockedApi.routeAudit.mockRejectedValueOnce(new Error("timeout"))
    mockedApi.routeAudit.mockResolvedValueOnce([AUDIT_1])
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText(/Falha ao carregar auditoria/i)).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText("Tentar novamente"))
    await waitFor(() => {
      expect(mockedApi.routeAudit).toHaveBeenCalledTimes(2)
    })
  })
})

describe("RouteAuditPanel — lista vazia", () => {
  it("exibe mensagem quando não há entradas", async () => {
    mockedApi.routeAudit.mockResolvedValue([])
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText(/Nenhuma entrada de auditoria/i)).toBeInTheDocument()
    })
  })
})

describe("RouteAuditPanel — rollback", () => {
  it("clicar em Reverter abre ConfirmDialog", async () => {
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText("admin@co.test")).toBeInTheDocument()
    })
    const listBtns = screen.getAllByRole("button", { name: /Reverter rota para snapshot/i })
    fireEvent.click(listBtns[0])
    await waitFor(() => {
      expect(screen.getByText(/Reverter "Rota A" para/i)).toBeInTheDocument()
    })
  })

  it("confirmar rollback chama rollbackRoute com routeId e auditId corretos", async () => {
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText("admin@co.test")).toBeInTheDocument()
    })

    const listBtns = screen.getAllByRole("button", { name: /Reverter rota para snapshot/i })
    fireEvent.click(listBtns[0])

    // Aguarda o ConfirmDialog abrir
    await waitFor(() => {
      expect(screen.getByText(/Reverter "Rota A" para/i)).toBeInTheDocument()
    })

    // Clica no botão de confirmação do dialog
    const confirmBtn = await screen.findByRole("button", { name: /^Reverter$/ })
    fireEvent.click(confirmBtn)

    await waitFor(() => {
      expect(mockedApi.rollbackRoute).toHaveBeenCalledWith("r-1", "audit-001")
    })
  })

  it("após rollback bem-sucedido, chama onRolledBack", async () => {
    const onRolledBack = vi.fn()
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" onRolledBack={onRolledBack} />)
    await waitFor(() => {
      expect(screen.getByText("admin@co.test")).toBeInTheDocument()
    })

    const listBtns = screen.getAllByRole("button", { name: /Reverter rota para snapshot/i })
    fireEvent.click(listBtns[0])

    await waitFor(() => {
      expect(screen.getByText(/Reverter "Rota A" para/i)).toBeInTheDocument()
    })

    const confirmBtn = await screen.findByRole("button", { name: /^Reverter$/ })
    fireEvent.click(confirmBtn)

    await waitFor(() => {
      expect(onRolledBack).toHaveBeenCalledTimes(1)
    })
  })

  it("exibe toast de erro quando rollbackRoute falha", async () => {
    mockedApi.rollbackRoute.mockRejectedValue(new Error("Rota bloqueada"))
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText("admin@co.test")).toBeInTheDocument()
    })

    const listBtns = screen.getAllByRole("button", { name: /Reverter rota para snapshot/i })
    fireEvent.click(listBtns[0])

    await waitFor(() => {
      expect(screen.getByText(/Reverter "Rota A" para/i)).toBeInTheDocument()
    })

    const confirmBtn = await screen.findByRole("button", { name: /^Reverter$/ })
    fireEvent.click(confirmBtn)

    await waitFor(() => {
      expect(screen.getByText(/Rota bloqueada/i)).toBeInTheDocument()
    })
  })

  it("entrada com action=deleted não tem botão Reverter", async () => {
    const deletedEntry: RouteAudit = {
      ...AUDIT_1,
      id: "audit-del",
      action: "deleted",
    }
    mockedApi.routeAudit.mockResolvedValue([deletedEntry])
    render(<RouteAuditPanel routeId="r-1" routeName="Rota A" />)
    await waitFor(() => {
      expect(screen.getByText("excluída")).toBeInTheDocument()
    })
    expect(screen.queryByRole("button", { name: /Reverter rota para snapshot/i })).not.toBeInTheDocument()
  })
})
