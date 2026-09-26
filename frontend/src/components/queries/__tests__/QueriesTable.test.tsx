/**
 * QueriesTable — R2-8.9: sem teste dedicado antes E 100% string hardcoded em
 * PT (título/descrição/colunas/ações), invisível para en/es apesar do resto
 * do módulo (`CreateQueryForm`/`EditQueryModal`) já ser i18n desde a Rodada 1.
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { QueriesTable } from "@/components/queries/QueriesTable"
import type { Query } from "@/types"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function query(over: Partial<Query> = {}): Query {
  return {
    id: 1,
    title: "Falhas de auth",
    description: "Logins recusados na última hora",
    statement: "SELECT * FROM events WHERE event_type = 'auth_failure'",
    client_ids: [],
    severity: "medium",
    finding_shape: "list",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...over,
  } as Query
}

describe("QueriesTable — estados", () => {
  it("loading mostra o spinner com texto traduzido", () => {
    render(<QueriesTable queries={[]} loading onEdit={vi.fn()} onDelete={vi.fn()} />)
    expect(screen.getByText("Carregando queries...")).toBeInTheDocument()
  })

  it("lista vazia mostra EmptyState descritivo", () => {
    render(<QueriesTable queries={[]} onEdit={vi.fn()} onDelete={vi.fn()} />)
    expect(screen.getByText("Nenhuma query salva")).toBeInTheDocument()
    expect(
      screen.getByText("Crie consultas reutilizáveis para acelerar buscas e automatizações."),
    ).toBeInTheDocument()
  })

  it("com queries, mostra colunas traduzidas e dados da linha", () => {
    render(<QueriesTable queries={[query()]} onEdit={vi.fn()} onDelete={vi.fn()} />)

    expect(screen.getByRole("table", { name: "Lista de queries salvas" })).toBeInTheDocument()
    expect(screen.getByText("Descrição")).toBeInTheDocument()
    expect(screen.getByText("Ações")).toBeInTheDocument()
    expect(screen.getAllByText("Falhas de auth").length).toBeGreaterThan(0)
    expect(screen.getAllByText("ID #1").length).toBeGreaterThan(0)
    expect(screen.getAllByText("Nenhum padrão").length).toBeGreaterThan(0)
  })

  it("cliente único usa singular; múltiplos usam plural (i18next count)", () => {
    render(
      <QueriesTable queries={[query({ id: 2, client_ids: [10] })]} onEdit={vi.fn()} onDelete={vi.fn()} />,
    )
    expect(screen.getAllByText("1 cliente").length).toBeGreaterThan(0)
  })

  it("sem descrição cai no fallback traduzido, não string vazia", () => {
    render(<QueriesTable queries={[query({ description: undefined })]} onEdit={vi.fn()} onDelete={vi.fn()} />)
    expect(screen.getAllByText("Sem descrição").length).toBeGreaterThan(0)
  })
})

describe("QueriesTable — interação", () => {
  it("clicar em Editar chama onEdit com a query certa", () => {
    const onEdit = vi.fn()
    render(<QueriesTable queries={[query()]} onEdit={onEdit} onDelete={vi.fn()} />)

    fireEvent.click(screen.getAllByRole("button", { name: "Editar" })[0])
    expect(onEdit).toHaveBeenCalledWith(expect.objectContaining({ id: 1 }))
  })

  it("clicar em Remover chama onDelete com o id certo", () => {
    const onDelete = vi.fn()
    render(<QueriesTable queries={[query()]} onEdit={vi.fn()} onDelete={onDelete} />)

    fireEvent.click(screen.getAllByRole("button", { name: "Remover" })[0])
    expect(onDelete).toHaveBeenCalledWith(1)
  })
})
