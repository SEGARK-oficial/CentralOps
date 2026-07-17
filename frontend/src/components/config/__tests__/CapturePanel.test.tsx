/**
 * Testes de CapturePanel (captura ao vivo / "modo escuta").
 * Cobre: empty-state, iniciar sessão, listar sessões, ver eventos da sessão,
 * parar sessão, a mensagem amigável de limite (429), e o seletor de organização
 * exigido para o admin GLOBAL (captura é por-tenant → admin global escolhe a org
 * de destino; admin escopado herda a própria org, sem seletor).
 */

import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { CapturePanel } from "@/components/config/CapturePanel"
import * as api from "@/services/api"
import { ApiRequestError } from "@/services/api"
import { useAuth } from "@/contexts/AuthContext"
import type { CaptureSession, Organization } from "@/types"
import { vi } from "vitest"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// Mantém a classe real ApiRequestError (o auto-mock apagaria o construtor →
// statusCode undefined, quebrando a detecção de 429). Mocka só as funções.
vi.mock("@/services/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/services/api")>()
  return {
    ...actual,
    listCaptureSessions: vi.fn(),
    startCaptureSession: vi.fn(),
    getCaptureEvents: vi.fn(),
    stopCaptureSession: vi.fn(),
    deleteCaptureSession: vi.fn(),
    listPlatformsStreams: vi.fn(),
    listOrganizations: vi.fn(),
  }
})
const mockedApi = vi.mocked(api)

// CapturePanel lê o usuário do AuthContext p/ decidir se mostra o seletor de org.
vi.mock("@/contexts/AuthContext")
const mockedUseAuth = vi.mocked(useAuth)

// Admin ESCOPADO: tem organization_id → herda a própria org, sem seletor.
const scopedAdmin = {
  id: "1",
  username: "admin",
  role: "admin" as const,
  is_active: true,
  permissions: [],
  organization_id: 42,
  is_global: false,
}
// Admin GLOBAL: sem org (is_global / organization_id null) → precisa escolher.
const globalAdmin = {
  id: "9",
  username: "root",
  role: "admin" as const,
  is_active: true,
  permissions: [],
  organization_id: null,
  is_global: true,
}

const acmeOrg: Organization = {
  id: 7,
  name: "Acme Corp",
  slug: "acme",
  is_active: true,
  integration_count: 0,
}

const activeSession: CaptureSession = {
  id: "cap-1",
  vendor: "sophos",
  status: "active",
  event_count: 2,
  created_at: 1_714_000_000,
  expires_at: 1_714_000_300,
}

function baseMocks() {
  mockedApi.listPlatformsStreams.mockResolvedValue({
    platforms: { sophos: ["sophos.alert"], defender: ["defender.incident"] },
  })
  mockedApi.listCaptureSessions.mockResolvedValue({ count: 0, sessions: [] })
  mockedApi.listOrganizations.mockResolvedValue([acmeOrg])
}

describe("CapturePanel", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Padrão: admin escopado (comportamento clássico, sem seletor de org).
    mockedUseAuth.mockReturnValue({ user: scopedAdmin } as never)
  })

  it("mostra empty-state quando não há sessões", async () => {
    baseMocks()
    render(<CapturePanel />)
    await waitFor(() =>
      expect(screen.getByText("Nenhuma sessão de captura")).toBeInTheDocument(),
    )
  })

  it("popula o select de vendor a partir do catálogo", async () => {
    baseMocks()
    render(<CapturePanel />)
    const select = await screen.findByRole("combobox", { name: /vendor da captura/i })
    await waitFor(() => {
      const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent)
      expect(options).toContain("sophos")
      expect(options).toContain("defender")
      expect(options).toContain("Todos os vendors")
    })
  })

  it("inicia uma sessão e recarrega a lista", async () => {
    baseMocks()
    mockedApi.startCaptureSession.mockResolvedValue(activeSession)
    // Após iniciar, a sessão aparece na lista.
    mockedApi.listCaptureSessions
      .mockResolvedValueOnce({ count: 0, sessions: [] })
      .mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({ count: 0, session_id: "cap-1", events: [] })

    render(<CapturePanel />)
    await waitFor(() => screen.getByText("Iniciar captura"))
    fireEvent.click(screen.getByText("Iniciar captura"))

    await waitFor(() => expect(mockedApi.startCaptureSession).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(screen.getByText(/Captura iniciada/i)).toBeInTheDocument(),
    )
  })

  it("lista sessões existentes e exibe o status", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    render(<CapturePanel />)
    await waitFor(() => expect(screen.getByText("active")).toBeInTheDocument())
    // vendor da sessão na coluna
    expect(screen.getAllByText("sophos").length).toBeGreaterThanOrEqual(1)
  })

  it("carrega eventos ao clicar em Eventos", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 1,
      session_id: "cap-1",
      events: [{ event: { id: "evt-xyz", severity: "high" }, vendor: "sophos", captured_at: 1_714_000_100 }],
    })

    render(<CapturePanel />)
    // "Eventos" também é cabeçalho de coluna (contagem) → desambigua por role.
    const eventosBtn = await screen.findByRole("button", { name: /eventos/i })
    fireEvent.click(eventosBtn)

    // Admin escopado → sem org_id explícito (backend herda a própria org).
    await waitFor(() =>
      expect(mockedApi.getCaptureEvents).toHaveBeenCalledWith("cap-1", 500, undefined),
    )
    await waitFor(() =>
      expect(screen.getByText(/evt-xyz/)).toBeInTheDocument(),
    )
  })

  it("para uma sessão ativa", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.stopCaptureSession.mockResolvedValue(undefined as never)

    render(<CapturePanel />)
    await waitFor(() => screen.getByText("Parar"))
    fireEvent.click(screen.getByText("Parar"))

    await waitFor(() => expect(mockedApi.stopCaptureSession).toHaveBeenCalledWith("cap-1", undefined))
  })

  it("exclui a sessão selecionada e limpa a visão de eventos (sem crash)", async () => {
    baseMocks()
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 1,
      session_id: "cap-1",
      events: [{ event: { id: "evt-del" }, vendor: "sophos", captured_at: 1_714_000_100 }],
    })
    mockedApi.deleteCaptureSession.mockResolvedValue(undefined as never)
    // 1ª listagem: sessão presente; após excluir, listagem vazia.
    mockedApi.listCaptureSessions
      .mockResolvedValueOnce({ count: 1, sessions: [activeSession] })
      .mockResolvedValue({ count: 0, sessions: [] })

    render(<CapturePanel />)
    // Seleciona a sessão (abre a visão de eventos).
    const eventosBtn = await screen.findByRole("button", { name: /eventos/i })
    fireEvent.click(eventosBtn)
    await waitFor(() => expect(screen.getByText(/evt-del/)).toBeInTheDocument())

    // Excluir → ConfirmDialog → confirmar (escopado ao diálogo: o botão da
    // linha e o de confirmação têm o mesmo rótulo "Excluir").
    fireEvent.click(screen.getByRole("button", { name: /excluir/i }))
    const dialog = await screen.findByRole("dialog")
    fireEvent.click(within(dialog).getByRole("button", { name: /excluir/i }))

    await waitFor(() => expect(mockedApi.deleteCaptureSession).toHaveBeenCalledWith("cap-1", undefined))
    // A visão de eventos some e volta o empty-state — sem exceção.
    await waitFor(() =>
      expect(screen.getByText("Nenhuma sessão de captura")).toBeInTheDocument(),
    )
  })

  it("mostra mensagem amigável quando o limite (429) é atingido", async () => {
    baseMocks()
    mockedApi.startCaptureSession.mockRejectedValue(
      new ApiRequestError("limit reached", 429),
    )
    render(<CapturePanel />)
    await waitFor(() => screen.getByText("Iniciar captura"))
    fireEvent.click(screen.getByText("Iniciar captura"))

    await waitFor(() =>
      expect(screen.getByText(/Limite de sessões de captura simultâneas/i)).toBeInTheDocument(),
    )
  })

  // ── Admin GLOBAL: seletor de organização ────────────────────────────────────

  it("admin escopado NÃO vê o seletor de organização", async () => {
    baseMocks()
    render(<CapturePanel />)
    await waitFor(() => screen.getByText("Iniciar captura"))
    expect(screen.queryByRole("combobox", { name: /organização da captura/i })).toBeNull()
    // O botão iniciar está habilitado (org implícita).
    expect(screen.getByRole("button", { name: /iniciar captura/i })).toBeEnabled()
    expect(mockedApi.listOrganizations).not.toHaveBeenCalled()
  })

  it("admin global sem org: botão desabilitado + hint, e não inicia captura", async () => {
    baseMocks()
    mockedApi.startCaptureSession.mockResolvedValue(activeSession)
    mockedUseAuth.mockReturnValue({ user: globalAdmin } as never)

    render(<CapturePanel />)

    const startBtn = await screen.findByRole("button", { name: /iniciar captura/i })
    expect(startBtn).toBeDisabled()
    expect(
      screen.getByText(/Selecione uma organização para capturar o tráfego dela/i),
    ).toBeInTheDocument()

    fireEvent.click(startBtn)
    expect(mockedApi.startCaptureSession).not.toHaveBeenCalled()
  })

  it("admin global seleciona a org → start chamado com org_id", async () => {
    baseMocks()
    mockedApi.startCaptureSession.mockResolvedValue(activeSession)
    mockedUseAuth.mockReturnValue({ user: globalAdmin } as never)

    render(<CapturePanel />)

    const orgSelect = await screen.findByRole("combobox", { name: /organização da captura/i })
    // A opção aparece assim que listOrganizations resolve.
    await waitFor(() =>
      expect(within(orgSelect).getByRole("option", { name: "Acme Corp" })).toBeInTheDocument(),
    )
    fireEvent.change(orgSelect, { target: { value: "7" } })

    const startBtn = screen.getByRole("button", { name: /iniciar captura/i })
    await waitFor(() => expect(startBtn).toBeEnabled())
    fireEvent.click(startBtn)

    await waitFor(() =>
      expect(mockedApi.startCaptureSession).toHaveBeenCalledWith(
        { vendor: undefined, duration_seconds: 300, ring_size: 5000 },
        7,
      ),
    )
    // A listagem também passa a ser escopada à org escolhida.
    await waitFor(() => expect(mockedApi.listCaptureSessions).toHaveBeenCalledWith(7))
  })
})
