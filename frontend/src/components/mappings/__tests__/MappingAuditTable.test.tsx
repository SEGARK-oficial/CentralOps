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

const AVAILABLE = ["create_version", "rollback", "ignore_field"]

const ENTRIES: MappingAuditEntry[] = [
  {
    id: "a1",
    mapping_definition_id: "m1",
    mapping_version_id: "v1",
    action: "create_version",
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
  mockedUseAudit.mockReturnValue({ entries: ENTRIES, isLoading: false, error: null, availableActions: AVAILABLE })
})

describe("MappingAuditTable", () => {
  it("renderiza entradas de auditoria", () => {
    render(<MappingAuditTable mappingId="m1" />)

    expect(screen.getByText("create_version")).toBeInTheDocument()
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
    mockedUseAudit.mockReturnValue({ entries: [], isLoading: true, error: null, availableActions: AVAILABLE })
    render(<MappingAuditTable mappingId="m1" />)
    // O DataTable não escreve mais "Carregando dados..." ao lado do spinner:
    // o spinner já diz isso. O anúncio para leitor de tela continua, via o
    // texto sr-only dentro do role="status" (que NÃO deriva nome do conteúdo,
    // por isso a asserção é no conteúdo e não no accessible name).
    const status = screen.getByRole("status")
    expect(status).toHaveAttribute("aria-busy", "true")
    expect(status).toHaveTextContent(/carregando/i)
  })

  it("exibe notice de erro quando error está presente", () => {
    mockedUseAudit.mockReturnValue({
      entries: [],
      availableActions: AVAILABLE,
      isLoading: false,
      error: new Error("Falha ao carregar"),
    })
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("Erro ao carregar auditoria")).toBeInTheDocument()
    expect(screen.getByText("Falha ao carregar")).toBeInTheDocument()
  })

  it("exibe mensagem de vazio quando sem entries", () => {
    mockedUseAudit.mockReturnValue({ entries: [], isLoading: false, error: null, availableActions: AVAILABLE })
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByText("Nenhum registro de auditoria encontrado")).toBeInTheDocument()
  })

  it("monta o seletor de ação a partir do backend, não de uma lista local", () => {
    render(<MappingAuditTable mappingId="m1" />)
    fireEvent.click(screen.getByLabelText("Filtrar por ação"))
    const labels = screen.getAllByRole("option").map((o) => o.textContent?.trim())

    // Uma opção por ação servida pelo backend, mais "todas".
    expect(labels).toHaveLength(AVAILABLE.length + 1)
    expect(labels).toContain("Versão criada") // create_version, traduzido
    expect(labels).toContain("Campo ignorado") // ignore_field, traduzido

    // E nenhuma das três que o backend NUNCA grava. Como o filtro é igualdade
    // exata server-side, oferecê-las devolvia tabela vazia com HTTP 200 —
    // lido pelo operador como "não houve atividade".
    expect(labels).not.toContain("version_created")
    expect(labels).not.toContain("drift_detected")
    expect(labels).not.toContain("quarantine")
  })

  it("não quebra quando o backend não serve available_actions", () => {
    // Backend antigo (campo ausente) → só a opção "todas", sem crash.
    mockedUseAudit.mockReturnValue({
      entries: ENTRIES,
      isLoading: false,
      error: null,
      availableActions: [],
    })
    render(<MappingAuditTable mappingId="m1" />)
    expect(screen.getByLabelText("Filtrar por ação")).toBeInTheDocument()
  })
})
