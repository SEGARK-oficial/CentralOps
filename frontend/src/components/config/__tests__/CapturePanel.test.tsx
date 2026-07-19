/**
 * Testes de CapturePanel (captura ao vivo / "modo escuta").
 * Cobre: empty-state, iniciar sessão, listar sessões, ver eventos da sessão,
 * parar sessão, a mensagem amigável de limite (429), e o seletor de organização
 * exigido para o admin GLOBAL (captura é por-tenant → admin global escolhe a org
 * de destino; admin escopado herda a própria org, sem seletor).
 *
 * Cobre também o troubleshooting "como entrou e como saiu":
 *  - estado vazio HONESTO (sessão ativa e nada capturado → explica o porquê);
 *  - desfecho (outcome) por evento: badge, filtro, e resiliência ao campo ausente;
 *  - janela padrão de 15 min (5 min cabe entre dois ciclos de coleta);
 *  - auto-seleção da sessão mais recente ao montar + busca final ao expirar.
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

/** Mesma sessão já encerrada (janela expirou) — usado no teste de busca final. */
const expiredSession: CaptureSession = { ...activeSession, status: "expired" }

function baseMocks() {
  mockedApi.listPlatformsStreams.mockResolvedValue({
    platforms: { sophos: ["sophos.alert"], defender: ["defender.incident"] },
  })
  mockedApi.listCaptureSessions.mockResolvedValue({ count: 0, sessions: [] })
  mockedApi.listOrganizations.mockResolvedValue([acmeOrg])
  // O painel auto-seleciona a sessão mais recente ao montar → qualquer teste
  // com sessão na lista dispara getCaptureEvents sem clique.
  mockedApi.getCaptureEvents.mockResolvedValue({ count: 0, session_id: "cap-1", events: [] })
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
        // Janela padrão = 15 min (ver teste dedicado abaixo).
        { vendor: undefined, duration_seconds: 900, ring_size: 5000 },
        7,
      ),
    )
    // A listagem também passa a ser escopada à org escolhida.
    await waitFor(() => expect(mockedApi.listCaptureSessions).toHaveBeenCalledWith(7))
  })

  // ── Janela padrão (C3) ──────────────────────────────────────────────────────

  it("usa janela padrão de 15 min (5 min cabe entre dois ciclos de coleta)", async () => {
    baseMocks()
    mockedApi.startCaptureSession.mockResolvedValue(activeSession)

    render(<CapturePanel />)
    fireEvent.click(await screen.findByRole("button", { name: /iniciar captura/i }))

    await waitFor(() =>
      expect(mockedApi.startCaptureSession).toHaveBeenCalledWith(
        { vendor: undefined, duration_seconds: 900, ring_size: 5000 },
        undefined,
      ),
    )
    // O select reflete o default, e a relação janela × cadência é explícita.
    expect(screen.getByRole("combobox", { name: /duração da captura/i })).toHaveValue("900")
    expect(screen.getByText(/coletores rodam em ciclos/i)).toBeInTheDocument()
  })

  // ── Estado vazio honesto (C1) ───────────────────────────────────────────────

  it("sessão ativa sem eventos EXPLICA o motivo em vez de mostrar vazio mudo", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })

    render(<CapturePanel />)

    await waitFor(() =>
      expect(screen.getByText("Sessão ativa — aguardando eventos")).toBeInTheDocument(),
    )
    // O "porquê": captura reflete o pipeline + coletores rodam em ciclos.
    expect(screen.getByText(/a captura registra o que o pipeline processa/i)).toBeInTheDocument()
    // "ciclos" aparece 2x: no hint do formulário e no porquê do estado vazio.
    expect(screen.getAllByText(/rodam em ciclos/i).length).toBeGreaterThanOrEqual(2)
    // Janela da sessão (expires_at - created_at = 300s → ~5 min) e filtro de vendor.
    expect(screen.getByText(/Janela desta sessão: ~5 min/)).toBeInTheDocument()
    expect(screen.getByText(/vendor “sophos”/)).toBeInTheDocument()
  })

  it("usa os contadores por desfecho do backend para desmentir o 'vazio'", async () => {
    baseMocks()
    // Backend expõe contadores agregados: houve tráfego, a lista é que está vazia.
    const withCounts = {
      ...activeSession,
      outcome_counts: { delivered: 3, dropped: 2 },
    } as CaptureSession
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [withCounts] })

    render(<CapturePanel />)

    await waitFor(() =>
      expect(screen.getByText(/já contabilizou 5 evento/i)).toBeInTheDocument(),
    )
  })

  it("sessão encerrada sem eventos sugere repetir com janela maior", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [expiredSession] })

    render(<CapturePanel />)

    await waitFor(() => expect(screen.getByText("Sem eventos capturados")).toBeInTheDocument())
    expect(screen.getByText(/janela maior \(15 min ou mais\)/i)).toBeInTheDocument()
  })

  // ── Desfecho por evento (C2) ────────────────────────────────────────────────

  it("mostra o desfecho de cada evento e permite filtrar por ele", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 3,
      session_id: "cap-1",
      events: [
        { event: { id: "evt-ok" }, vendor: "sophos", captured_at: 1, outcome: "delivered", destination_id: 12 },
        { event: { id: "evt-drop" }, vendor: "sophos", captured_at: 2, outcome: "dropped", detail: "sem regra" },
        { event: { id: "evt-quar" }, vendor: "sophos", captured_at: 3, outcome: "quarantined" },
      ] as never,
    })

    render(<CapturePanel />)

    // Badges por desfecho + coluna dedicada.
    await waitFor(() => expect(screen.getByText("Desfecho")).toBeInTheDocument())
    expect(screen.getAllByText("Entregue").length).toBeGreaterThanOrEqual(1)
    // regex: o rótulo é "Descartado (drop)" — o sufixo distingue a AÇÃO DE ROTA de
    // outros descartes (sampled_out/suppressed também somem do destino).
    expect(screen.getAllByText(/^Descartado/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText("Quarentena").length).toBeGreaterThanOrEqual(1)
    // Destino do evento entregue aparece junto do badge.
    expect(screen.getByText(/destino: 12/)).toBeInTheDocument()

    // Filtra por "dropped": só o evento descartado permanece na tabela.
    fireEvent.change(screen.getByRole("combobox", { name: /filtrar por desfecho/i }), {
      target: { value: "dropped" },
    })
    await waitFor(() => expect(screen.queryByText(/evt-ok/)).toBeNull())
    expect(screen.getByText(/evt-drop/)).toBeInTheDocument()
    expect(screen.queryByText(/evt-quar/)).toBeNull()
  })

  it("filtro sem resultado não se disfarça de 'sem tráfego'", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 2,
      session_id: "cap-1",
      events: [
        { event: { id: "evt-alfa" }, vendor: "sophos", captured_at: 1, outcome: "delivered" },
        { event: { id: "evt-beta" }, vendor: "sophos", captured_at: 2, outcome: "delivered" },
      ] as never,
    })

    render(<CapturePanel />)
    const filter = await screen.findByRole("combobox", { name: /filtrar por desfecho/i })
    // Só existe "delivered"; escolher outro valor via chip não é possível, então
    // simulamos a troca direta para um desfecho ausente do conjunto atual.
    fireEvent.change(filter, { target: { value: "delivered" } })
    await waitFor(() => expect(screen.getByText(/evt-alfa/)).toBeInTheDocument())

    // Agora o refresh traz só eventos de outro desfecho → o filtro esconde tudo.
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 1,
      session_id: "cap-1",
      events: [{ event: { id: "evt-drop" }, vendor: "sophos", captured_at: 3, outcome: "dropped" }] as never,
    })
    fireEvent.click(screen.getAllByRole("button", { name: /atualizar/i })[1])

    await waitFor(() =>
      expect(screen.getByText(/Nenhum evento com o desfecho/i)).toBeInTheDocument(),
    )
    // E dá para limpar o filtro em um clique.
    fireEvent.click(screen.getByRole("button", { name: /limpar filtro/i }))
    await waitFor(() => expect(screen.getByText(/evt-drop/)).toBeInTheDocument())
  })

  it("evento antigo sem desfecho não quebra a renderização", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 1,
      session_id: "cap-1",
      // Ring antigo: nenhum campo `outcome`.
      events: [{ event: { id: "evt-legacy" }, vendor: "sophos", captured_at: 1 }],
    })

    render(<CapturePanel />)

    await waitFor(() => expect(screen.getByText(/evt-legacy/)).toBeInTheDocument())
    // Sem desfecho em NENHUM evento → nem coluna nem filtro (UI idêntica à antiga).
    expect(screen.queryByText("Desfecho")).toBeNull()
    expect(screen.queryByRole("combobox", { name: /filtrar por desfecho/i })).toBeNull()
  })

  it("desfecho desconhecido cai no rótulo cru sem quebrar", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 1,
      session_id: "cap-1",
      events: [
        { event: { id: "evt-novo" }, vendor: "sophos", captured_at: 1, outcome: "algum_desfecho_novo" },
      ] as never,
    })

    render(<CapturePanel />)
    await waitFor(() =>
      expect(screen.getAllByText("algum_desfecho_novo").length).toBeGreaterThanOrEqual(1),
    )
  })

  // ── Auto-seleção e busca final (C4) ─────────────────────────────────────────

  it("auto-seleciona a sessão mais recente ao montar (sem clique)", async () => {
    baseMocks()
    mockedApi.listCaptureSessions.mockResolvedValue({ count: 1, sessions: [activeSession] })
    mockedApi.getCaptureEvents.mockResolvedValue({
      count: 1,
      session_id: "cap-1",
      events: [{ event: { id: "evt-restaurado" }, vendor: "sophos", captured_at: 1 }],
    })

    render(<CapturePanel />)

    // Sem nenhuma interação: os eventos da sessão já aparecem (antes, um reload
    // deixava a tela vazia mesmo com os eventos vivos no Redis).
    await waitFor(() =>
      expect(mockedApi.getCaptureEvents).toHaveBeenCalledWith("cap-1", 500, undefined),
    )
    expect(await screen.findByText(/evt-restaurado/)).toBeInTheDocument()
  })

  it("prefere a sessão ATIVA na auto-seleção", async () => {
    baseMocks()
    const older = { ...expiredSession, id: "cap-old" }
    mockedApi.listCaptureSessions.mockResolvedValue({
      count: 2,
      sessions: [older, activeSession],
    })

    render(<CapturePanel />)
    await waitFor(() =>
      expect(mockedApi.getCaptureEvents).toHaveBeenCalledWith("cap-1", 500, undefined),
    )
    expect(mockedApi.getCaptureEvents).not.toHaveBeenCalledWith("cap-old", 500, undefined)
  })

  it("faz uma busca FINAL dos eventos quando a sessão deixa de estar ativa", async () => {
    baseMocks()
    // 1ª listagem: ativa. Depois de parar: encerrada.
    mockedApi.listCaptureSessions
      .mockResolvedValueOnce({ count: 1, sessions: [activeSession] })
      .mockResolvedValue({ count: 1, sessions: [{ ...activeSession, status: "stopped" }] })
    mockedApi.stopCaptureSession.mockResolvedValue(undefined as never)
    // A auto-seleção pega o ring ainda vazio; a busca final é que traz o evento
    // gravado no fim da janela.
    mockedApi.getCaptureEvents
      .mockResolvedValueOnce({ count: 0, session_id: "cap-1", events: [] })
      .mockResolvedValue({
        count: 1,
        session_id: "cap-1",
        events: [{ event: { id: "evt-final" }, vendor: "sophos", captured_at: 9 }],
      })

    render(<CapturePanel />)
    await waitFor(() => expect(mockedApi.getCaptureEvents).toHaveBeenCalledTimes(1))

    fireEvent.click(screen.getByText("Parar"))

    // Nenhum clique em "Atualizar" dos eventos: quem buscou foi a transição
    // ativa → encerrada.
    await waitFor(() => expect(screen.getByText(/evt-final/)).toBeInTheDocument())
  })

  it("faz a busca final também quando a janela EXPIRA", async () => {
    baseMocks()
    mockedApi.listCaptureSessions
      .mockResolvedValueOnce({ count: 1, sessions: [activeSession] })
      .mockResolvedValue({ count: 1, sessions: [expiredSession] })
    mockedApi.getCaptureEvents
      .mockResolvedValueOnce({ count: 0, session_id: "cap-1", events: [] })
      .mockResolvedValue({
        count: 1,
        session_id: "cap-1",
        events: [{ event: { id: "evt-expirado" }, vendor: "sophos", captured_at: 9 }],
      })

    render(<CapturePanel />)
    await waitFor(() => expect(mockedApi.getCaptureEvents).toHaveBeenCalledTimes(1))

    // Recarrega a LISTA (botão do formulário, índice 0) → a sessão aparece como
    // expirada; a busca final dos eventos é disparada pela transição.
    fireEvent.click(screen.getAllByRole("button", { name: /atualizar/i })[0])

    await waitFor(() => expect(screen.getByText(/evt-expirado/)).toBeInTheDocument())
  })
})
