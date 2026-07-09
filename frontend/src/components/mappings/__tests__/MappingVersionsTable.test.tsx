/**
 * Testes de MappingVersionsTable
 * Cobre: render de N versões, badge "atual" no current, rollback gating,
 *        seleção de 2 versões para comparar.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MappingVersionsTable } from "@/components/mappings/MappingVersionsTable"
import * as permissionHooks from "@/hooks/usePermission"
import * as api from "@/services/api"
import type { MappingVersion } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/usePermission")
vi.mock("@/hooks/useMappingDiff", () => ({
  useMappingDiff: () => ({ diff: null, isLoading: false, error: null }),
}))
vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return { ...actual, rollbackMapping: vi.fn() }
})

const mockedUsePermission = vi.mocked(permissionHooks.usePermission)
const mockedApi = vi.mocked(api)

const V1: MappingVersion = {
  id: "v1",
  definition_id: "m1",
  version_number: 1,
  rules: { preprocess: [], rules: [{ target: "a", source: "x" }] },
  author_user_id: null,
  commit_message: "Versão inicial",
  diff_from_previous: null,
  dry_run_stats: null,
  created_at: "2026-01-01T00:00:00Z",
}

const V2: MappingVersion = {
  id: "v2",
  definition_id: "m1",
  version_number: 2,
  rules: { preprocess: [], rules: [{ target: "b", source: "y" }] },
  author_user_id: 1,
  commit_message: "Segunda versão",
  diff_from_previous: null,
  dry_run_stats: null,
  created_at: "2026-01-02T00:00:00Z",
}

function renderTable(currentVersionId: string | null = "v2") {
  return render(
    <MappingVersionsTable
      mappingId="m1"
      versions={[V1, V2]}
      currentVersionId={currentVersionId}
      onRefetch={vi.fn()}
    />,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUsePermission.mockReturnValue(false)
})

describe("MappingVersionsTable", () => {
  it("renderiza todas as versões", () => {
    renderTable()
    expect(screen.getByText("v1")).toBeInTheDocument()
    expect(screen.getByText("v2")).toBeInTheDocument()
  })

  it("badge 'atual' aparece na versão corrente", () => {
    renderTable("v2")
    // v2 é a atual
    expect(screen.getByText("atual")).toBeInTheDocument()
  })

  it("badge 'atual' aparece em v1 quando currentVersionId=v1", () => {
    renderTable("v1")
    expect(screen.getByText("atual")).toBeInTheDocument()
  })

  it("botão rollback NÃO aparece sem permissão mapping.rollback", () => {
    mockedUsePermission.mockReturnValue(false)
    renderTable("v2")
    expect(screen.queryByTestId("rollback-v1")).not.toBeInTheDocument()
  })

  it("botão rollback aparece com permissão mapping.rollback", () => {
    mockedUsePermission.mockReturnValue(true)
    renderTable("v2")
    // v1 não é a current, então deve ter botão
    expect(screen.getByTestId("rollback-v1")).toBeInTheDocument()
    // v2 é a current — sem botão
    expect(screen.queryByTestId("rollback-v2")).not.toBeInTheDocument()
  })

  it("clique em rollback abre ConfirmDialog", () => {
    mockedUsePermission.mockReturnValue(true)
    renderTable("v2")

    fireEvent.click(screen.getByTestId("rollback-v1"))

    // O título do dialog deve aparecer
    expect(screen.getByText(/Tornar v1 a versão atual/)).toBeInTheDocument()
  })

  it("rollback com commit message curto exibe erro de validação", async () => {
    mockedUsePermission.mockReturnValue(true)
    renderTable("v2")

    fireEvent.click(screen.getByTestId("rollback-v1"))

    // Preenche com mensagem curta
    const textarea = screen.getByPlaceholderText(/Motivo do rollback/)
    fireEvent.change(textarea, { target: { value: "curto" } })

    // Clica no botão de confirmação
    fireEvent.click(screen.getByRole("button", { name: /Confirmar rollback/i }))

    await waitFor(() => {
      expect(screen.getByText(/pelo menos 10 caracteres/)).toBeInTheDocument()
    })
    expect(mockedApi.rollbackMapping).not.toHaveBeenCalled()
  })

  it("rollback com commit válido chama rollbackMapping", async () => {
    mockedUsePermission.mockReturnValue(true)
    mockedApi.rollbackMapping.mockResolvedValue({} as any)
    renderTable("v2")

    fireEvent.click(screen.getByTestId("rollback-v1"))

    const textarea = screen.getByPlaceholderText(/Motivo do rollback/)
    fireEvent.change(textarea, { target: { value: "Rollback para versão anterior estável" } })

    fireEvent.click(screen.getByRole("button", { name: /Confirmar rollback/i }))

    await waitFor(() => {
      expect(mockedApi.rollbackMapping).toHaveBeenCalledWith("m1", {
        version_id: "v1",
        commit_message: "Rollback para versão anterior estável",
      })
    })
  })

  it("mensagem de commit exibida na coluna Mensagem", () => {
    renderTable()
    expect(screen.getByText("Versão inicial")).toBeInTheDocument()
    expect(screen.getByText("Segunda versão")).toBeInTheDocument()
  })

  it("seleção de 2 versões mostra botão 'Comparar selecionadas'", () => {
    renderTable("v2")
    const checkboxes = screen.getAllByRole("checkbox")
    fireEvent.click(checkboxes[0]) // seleciona v1
    fireEvent.click(checkboxes[1]) // seleciona v2
    expect(screen.getByText("Comparar selecionadas")).toBeInTheDocument()
  })
})
