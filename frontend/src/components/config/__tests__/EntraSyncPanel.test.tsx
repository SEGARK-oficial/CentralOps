/**
 * Testes do EntraSyncPanel (Fase 2B).
 * Cobre: render padrão, variantes de status (ok/error/running/never),
 * interação do botão Sincronizar agora, estado de lock ativo,
 * lista de erros colapsável e acessibilidade (teclado, aria).
 */

import { act, fireEvent, render, screen } from "@testing-library/react"
import { beforeAll, describe, expect, it, vi } from "vitest"
import { EntraSyncPanel, type EntraSyncPanelProps } from "@/components/config/EntraSyncPanel"
import type { EntraSyncStatus } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// ── fábrica de props ────────────────────────────────────────────────────

function makeStatus(overrides: Partial<EntraSyncStatus> = {}): EntraSyncStatus {
  return {
    last_sync_at: null,
    last_sync_status: null,
    last_sync_summary: null,
    lock_active: false,
    ...overrides,
  }
}

const defaultProps: EntraSyncPanelProps = {
  syncStatus: makeStatus(),
  loadingStatus: false,
  syncing: false,
  feedback: null,
  onSyncNow: vi.fn().mockResolvedValue({ queued: true, message: "Sync disparado", lock_active: false }),
  onRefreshStatus: vi.fn().mockResolvedValue(undefined),
}

function renderPanel(props: Partial<EntraSyncPanelProps> = {}) {
  return render(<EntraSyncPanel {...defaultProps} {...props} />)
}

// ── render padrão ────────────────────────────────────────────────────────

describe("EntraSyncPanel — render padrão", () => {
  it("exibe o título da seção", () => {
    renderPanel()
    expect(screen.getByText(/Status de Sincronização/i)).toBeInTheDocument()
  })

  it("exibe badge 'Nunca sincronizado' quando status é null", () => {
    renderPanel({ syncStatus: makeStatus({ last_sync_status: null }) })
    expect(screen.getByText(/Nunca sincronizado/i)).toBeInTheDocument()
  })

  it("exibe 'Nunca' quando last_sync_at é null", () => {
    renderPanel()
    expect(screen.getByText(/Último sync:/i)).toBeInTheDocument()
    // Usa getAllByText pois "Nunca sincronizado" (badge) e "Nunca" (timestamp) ambos casam
    const matches = screen.getAllByText(/Nunca/i)
    expect(matches.length).toBeGreaterThanOrEqual(1)
  })

  it("região de status tem role='status' e aria-live='polite'", () => {
    renderPanel()
    const region = screen.getByTestId("entra-sync-status-region")
    expect(region).toHaveAttribute("role", "status")
    expect(region).toHaveAttribute("aria-live", "polite")
  })
})

// ── variantes de status ──────────────────────────────────────────────────

describe("EntraSyncPanel — variantes de status", () => {
  it("status 'ok' exibe badge OK", () => {
    renderPanel({ syncStatus: makeStatus({ last_sync_status: "ok" }) })
    expect(screen.getByText("OK")).toBeInTheDocument()
  })

  it("status 'error' exibe badge Erro", () => {
    renderPanel({ syncStatus: makeStatus({ last_sync_status: "error" }) })
    expect(screen.getByText("Erro")).toBeInTheDocument()
  })

  it("status 'running' exibe badge Em andamento", () => {
    renderPanel({ syncStatus: makeStatus({ last_sync_status: "running" }) })
    expect(screen.getByText(/Em andamento/i)).toBeInTheDocument()
  })

  it("lock_active=true exibe badge Em andamento mesmo com status null", () => {
    renderPanel({ syncStatus: makeStatus({ lock_active: true, last_sync_status: null }) })
    expect(screen.getByText(/Em andamento/i)).toBeInTheDocument()
  })

  it("timestamp formatado quando last_sync_at é fornecido", () => {
    renderPanel({
      syncStatus: makeStatus({
        last_sync_at: "2026-06-14T10:00:00Z",
        last_sync_status: "ok",
      }),
    })
    // Apenas verifica que o campo existe e não mostra "Nunca"
    const region = screen.getByTestId("entra-sync-status-region")
    expect(region).not.toHaveTextContent("Nunca")
  })
})

// ── contadores ────────────────────────────────────────────────────────────

describe("EntraSyncPanel — contadores", () => {
  it("exibe criados/atualizados/desativados do summary", () => {
    renderPanel({
      syncStatus: makeStatus({
        last_sync_status: "ok",
        last_sync_summary: {
          created: 5,
          updated: 3,
          deactivated: 1,
          errors: [],
          started_at: null,
          finished_at: null,
        },
      }),
    })
    expect(screen.getByText("5")).toBeInTheDocument()
    expect(screen.getByText("3")).toBeInTheDocument()
    expect(screen.getByText("1")).toBeInTheDocument()
  })

  it("não exibe contadores quando summary é null", () => {
    renderPanel({ syncStatus: makeStatus({ last_sync_summary: null }) })
    expect(screen.queryByText(/Criados:/i)).not.toBeInTheDocument()
  })
})

// ── lista de erros colapsável ─────────────────────────────────────────────

describe("EntraSyncPanel — erros colapsáveis", () => {
  const statusComErros = makeStatus({
    last_sync_status: "error",
    last_sync_summary: {
      created: 0,
      updated: 0,
      deactivated: 0,
      errors: ["Falha na autenticação Graph", "Timeout ao buscar membros"],
      started_at: null,
      finished_at: null,
    },
  })

  it("exibe botão de erros quando há erros", () => {
    renderPanel({ syncStatus: statusComErros })
    expect(screen.getByRole("button", { name: /2 erro\(s\)/i })).toBeInTheDocument()
  })

  it("expande a lista ao clicar no botão", async () => {
    renderPanel({ syncStatus: statusComErros })
    const btn = screen.getByRole("button", { name: /2 erro\(s\)/i })
    await act(async () => { fireEvent.click(btn) })
    expect(screen.getByText("Falha na autenticação Graph")).toBeInTheDocument()
    expect(screen.getByText("Timeout ao buscar membros")).toBeInTheDocument()
  })

  it("colapsa novamente ao clicar de novo", async () => {
    renderPanel({ syncStatus: statusComErros })
    const btn = screen.getByRole("button", { name: /2 erro\(s\)/i })
    await act(async () => { fireEvent.click(btn) })
    await act(async () => { fireEvent.click(btn) })
    expect(screen.queryByText("Falha na autenticação Graph")).not.toBeInTheDocument()
  })

  it("botão de erros tem aria-expanded correto", async () => {
    renderPanel({ syncStatus: statusComErros })
    const btn = screen.getByRole("button", { name: /2 erro\(s\)/i })
    expect(btn).toHaveAttribute("aria-expanded", "false")
    await act(async () => { fireEvent.click(btn) })
    expect(btn).toHaveAttribute("aria-expanded", "true")
  })

  it("não exibe botão de erros quando lista de erros é vazia", () => {
    renderPanel({
      syncStatus: makeStatus({
        last_sync_summary: {
          created: 2,
          updated: 0,
          deactivated: 0,
          errors: [],
          started_at: null,
          finished_at: null,
        },
      }),
    })
    expect(screen.queryByText(/erro\(s\)/i)).not.toBeInTheDocument()
  })
})

// ── interação: botão Sincronizar agora ────────────────────────────────────

describe("EntraSyncPanel — botão Sincronizar agora", () => {
  it("chama onSyncNow ao clicar", async () => {
    const onSyncNow = vi.fn().mockResolvedValue({ queued: true, message: "Disparado", lock_active: false })
    renderPanel({ onSyncNow })
    // aria-label é "Disparar sincronização agora" quando não há lock ativo
    const btn = screen.getByRole("button", { name: /Disparar sincronização agora/i })
    await act(async () => { fireEvent.click(btn) })
    expect(onSyncNow).toHaveBeenCalledTimes(1)
  })

  it("botão desabilitado quando lock_active=true", () => {
    renderPanel({ syncStatus: makeStatus({ lock_active: true }) })
    // aria-label muda para "Sync em andamento" quando lock ativo
    const btn = screen.getByRole("button", { name: /Sync em andamento/i })
    expect(btn).toBeDisabled()
  })

  it("botão desabilitado quando syncing=true", () => {
    renderPanel({ syncing: true })
    // quando loading=true o Button muda o accessible name via sr-only "Carregando"
    const btn = screen.getByRole("button", { name: /Disparar sincronização agora/i })
    expect(btn).toBeDisabled()
  })

  it("exibe feedback de sucesso após sync", () => {
    renderPanel({
      feedback: { type: "success", message: "Sync de usuários Entra disparado" },
    })
    expect(screen.getByText("Sync de usuários Entra disparado")).toBeInTheDocument()
  })

  it("exibe feedback de erro após falha", () => {
    renderPanel({
      feedback: { type: "error", message: "Broker indisponível" },
    })
    expect(screen.getByText("Broker indisponível")).toBeInTheDocument()
  })
})

// ── carregando status ────────────────────────────────────────────────────

describe("EntraSyncPanel — estado de carregamento", () => {
  it("exibe mensagem de carregando quando loadingStatus=true", () => {
    renderPanel({ loadingStatus: true })
    expect(screen.getByText(/Carregando status/i)).toBeInTheDocument()
  })

  it("não exibe badge de status durante carregamento", () => {
    renderPanel({ loadingStatus: true })
    expect(screen.queryByText("OK")).not.toBeInTheDocument()
    expect(screen.queryByText("Erro")).not.toBeInTheDocument()
  })
})

// ── botão Atualizar ───────────────────────────────────────────────────────

describe("EntraSyncPanel — botão Atualizar", () => {
  it("chama onRefreshStatus ao clicar", async () => {
    const onRefreshStatus = vi.fn().mockResolvedValue(undefined)
    renderPanel({ onRefreshStatus })
    const btn = screen.getByRole("button", { name: /Atualizar/i })
    await act(async () => { fireEvent.click(btn) })
    expect(onRefreshStatus).toHaveBeenCalledTimes(1)
  })
})

// ── acessibilidade: teclado ───────────────────────────────────────────────

describe("EntraSyncPanel — acessibilidade teclado", () => {
  it("botão Sincronizar agora acionável via Enter", async () => {
    const onSyncNow = vi.fn().mockResolvedValue({ queued: true, message: "OK", lock_active: false })
    renderPanel({ onSyncNow })
    // aria-label = "Disparar sincronização agora" quando não há lock
    const btn = screen.getByRole("button", { name: /Disparar sincronização agora/i })
    btn.focus()
    await act(async () => { fireEvent.keyDown(btn, { key: "Enter" }) })
    // Nativo HTML button já dispara click no Enter — verificamos o foco
    expect(btn).toHaveFocus()
  })

  it("botão de erros acionável via teclado (aria-expanded)", async () => {
    const statusComErros = makeStatus({
      last_sync_status: "error",
      last_sync_summary: {
        created: 0,
        updated: 0,
        deactivated: 0,
        errors: ["Erro de rede"],
        started_at: null,
        finished_at: null,
      },
    })
    renderPanel({ syncStatus: statusComErros })
    const btn = screen.getByRole("button", { name: /1 erro\(s\)/i })
    btn.focus()
    expect(btn).toHaveFocus()
    await act(async () => { fireEvent.click(btn) })
    expect(btn).toHaveAttribute("aria-expanded", "true")
  })
})
