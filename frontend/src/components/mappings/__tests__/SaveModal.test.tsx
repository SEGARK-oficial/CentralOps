/**
 * Testes de SaveModal
 * Cobre: submit bloqueado sem commit, < 10 chars, diff renderizado,
 *        on success chama callbacks, on error mostra notice.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { SaveModal } from "@/components/mappings/SaveModal"
import * as api from "@/services/api"
import type { MappingPayload, MappingRule } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return { ...actual, createMappingVersion: vi.fn() }
})

const mockedApi = vi.mocked(api)

const RULES_A: MappingRule[] = [
  { target: "event.action", source: "action" },
  { target: "event.user", source: "user" },
]
const RULES_B: MappingRule[] = [
  { target: "event.action", source: "action_new" },
  { target: "event.user", source: "user" },
  { target: "event.host", source: "host" },
]
const PAYLOAD_B: MappingPayload = { preprocess: [], rules: RULES_B }

function renderModal(overrides: Partial<Parameters<typeof SaveModal>[0]> = {}) {
  const defaults = {
    open: true,
    onClose: vi.fn(),
    mappingId: "m1",
    currentRules: RULES_A,
    draftRules: RULES_B,
    draftPayload: PAYLOAD_B,
    currentVersionNumber: 1,
    onSuccess: vi.fn(),
    ...overrides,
  }
  return { ...render(<SaveModal {...defaults} />), props: defaults }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("SaveModal", () => {
  it("renderiza o modal quando open=true", () => {
    renderModal()
    expect(screen.getByTestId("save-modal")).toBeInTheDocument()
  })

  it("não renderiza quando open=false", () => {
    renderModal({ open: false })
    expect(screen.queryByTestId("save-modal")).not.toBeInTheDocument()
  })

  it("exibe resumo do diff com adicionadas e modificadas", () => {
    renderModal()
    // 1 adicionada (event.host), 1 modificada (event.action)
    expect(screen.getByText(/\+1 adicionada/)).toBeInTheDocument()
    expect(screen.getByText(/~1 modificada/)).toBeInTheDocument()
  })

  it("submit bloqueado sem commit message — exibe erro de validação", async () => {
    renderModal()
    fireEvent.click(screen.getByTestId("confirm-save"))
    await waitFor(() => {
      expect(screen.getByText("A mensagem do commit é obrigatória.")).toBeInTheDocument()
    })
    expect(mockedApi.createMappingVersion).not.toHaveBeenCalled()
  })

  it("submit bloqueado com commit < 10 chars — exibe erro de tamanho", async () => {
    renderModal()
    const input = screen.getByTestId("commit-message-input")
    fireEvent.change(input, { target: { value: "curto" } })
    fireEvent.click(screen.getByTestId("confirm-save"))
    await waitFor(() => {
      expect(screen.getByText(/pelo menos 10 caracteres/)).toBeInTheDocument()
    })
    expect(mockedApi.createMappingVersion).not.toHaveBeenCalled()
  })

  it("chama createMappingVersion e onSuccess com commit válido", async () => {
    const onSuccess = vi.fn()
    mockedApi.createMappingVersion.mockResolvedValue({} as any)

    renderModal({ onSuccess })

    const input = screen.getByTestId("commit-message-input")
    fireEvent.change(input, { target: { value: "Atualização de campos do evento" } })
    fireEvent.click(screen.getByTestId("confirm-save"))

    await waitFor(() => {
      expect(mockedApi.createMappingVersion).toHaveBeenCalledWith("m1", {
        rules: PAYLOAD_B,
        commit_message: "Atualização de campos do evento",
      })
    })
    expect(onSuccess).toHaveBeenCalled()
  })

  it("exibe Notice de erro quando backend retorna erro", async () => {
    mockedApi.createMappingVersion.mockRejectedValue(new Error("Erro interno do servidor"))

    renderModal()

    const input = screen.getByTestId("commit-message-input")
    fireEvent.change(input, { target: { value: "Commit message válida aqui" } })
    fireEvent.click(screen.getByTestId("confirm-save"))

    await waitFor(() => {
      expect(screen.getByText("Erro ao salvar")).toBeInTheDocument()
      expect(screen.getByText("Erro interno do servidor")).toBeInTheDocument()
    })
  })

  it("exibe warning de alto índice de falhas quando dryRunFailRatio > 0.5", () => {
    renderModal({ dryRunFailRatio: 0.8 })
    expect(screen.getByText("Alto índice de falhas na simulação")).toBeInTheDocument()
  })

  it("NÃO exibe warning de alto índice quando dryRunFailRatio <= 0.5", () => {
    renderModal({ dryRunFailRatio: 0.3 })
    expect(screen.queryByText("Alto índice de falhas na simulação")).not.toBeInTheDocument()
  })

  it("exibe Notice 'Sem alterações' quando currentRules === draftRules", () => {
    renderModal({ currentRules: RULES_A, draftRules: RULES_A })
    expect(screen.getByText("Nenhuma alteração em relação à versão atual.")).toBeInTheDocument()
  })
})
