/**
 * Testes de MappingAuditTable
 * Cobre: render de entries, filtros por ação e username.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { MappingAuditTable } from "@/components/mappings/MappingAuditTable"
import * as auditHooks from "@/hooks/useMappingAudit"
import type { MappingAuditEntry } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/useMappingAudit")
const mockedUseAudit = vi.mocked(auditHooks.useMappingAudit)

const ENTRIES: MappingAuditEntry[] = [
  {
    id: "a1",
    mapping_definition_id: "m1",
    mapping_version_id: "v1",
    action: "version_created",
    user_id: 1,
    username: "alice",
    user_role: "engineer",
    diff: null,
    detail: "Criou versão v1",
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    id: "a2",
    mapping_definition_id: "m1",
    mapping_version_id: "v2",
    action: "rollback",
    user_id: 2,
    username: "bob",
    user_role: "admin",
    diff: null,
    detail: "Rollback para v1",
    created_at: "2026-01-02T00:00:00Z",
  },
]

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseAudit.mockReturnValue({ entries: ENTRIES, isLoading: false, error: null })
})

describe("MappingAuditTable", () => {
  it("renderiza entradas de auditoria", () => {
    render(<MappingAuditTable mappingId="m1" />)

    expect(screen.getByText("version_created")).toBeInTheDocument()
    expect(screen.getByText("rollback")).toBeInTheDocument()
    expect(screen.getByText("alice")).toBeInTheDocument()
    expect(screen.getByText("bob")).toBeInTheDocument()
  })

  it("exibe papéis dos usuários", () => {
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("engineer")).toBeInTheDocument()
    expect(screen.getByText("admin")).toBeInTheDocument()
  })

  it("exibe detalhes (truncado)", () => {
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("Criou versão v1")).toBeInTheDocument()
  })

  it("filtro por username chama hook com username correto", () => {
    render(<MappingAuditTable mappingId="m1" />)

    const usernameInput = screen.getByPlaceholderText("Filtrar por usuário...")
    fireEvent.change(usernameInput, { target: { value: "alice" } })

    // O hook deve ter sido chamado com username: "alice"
    expect(mockedUseAudit).toHaveBeenLastCalledWith(
      "m1",
      expect.objectContaining({ username: "alice" }),
    )
  })

  it("exibe loading spinner quando isLoading=true", () => {
    mockedUseAudit.mockReturnValue({ entries: [], isLoading: true, error: null })
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("Carregando dados...")).toBeInTheDocument()
  })

  it("exibe notice de erro quando error está presente", () => {
    mockedUseAudit.mockReturnValue({
      entries: [],
      isLoading: false,
      error: new Error("Falha ao carregar"),
    })
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("Erro ao carregar auditoria")).toBeInTheDocument()
    expect(screen.getByText("Falha ao carregar")).toBeInTheDocument()
  })

  it("exibe mensagem de vazio quando sem entries", () => {
    mockedUseAudit.mockReturnValue({ entries: [], isLoading: false, error: null })
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("Nenhum registro de auditoria encontrado")).toBeInTheDocument()
  })
})
