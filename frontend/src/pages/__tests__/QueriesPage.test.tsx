/**
 * QueriesPage — Pilar 4: falha do carregamento inicial não pode virar o
 * EmptyState "nenhuma query salva" da QueriesTable (mensagem enganosa) e
 * precisa de retry.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { QueriesPage } from "@/pages/QueriesPage"
import * as api from "@/services/api"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listIntegrations.mockResolvedValue([])
  mockedApi.listQueryCapabilities.mockResolvedValue([])
})

describe("QueriesPage — Pilar 4 (ErrorState com retry)", () => {
  it("listQueries rejeitando no load inicial mostra ErrorState, não o EmptyState da tabela", async () => {
    mockedApi.listQueries.mockRejectedValue(new Error("503"))
    render(<QueriesPage />)

    await waitFor(() => expect(screen.getByText("503")).toBeInTheDocument())
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
    expect(screen.queryByText(/nenhuma query salva/i)).not.toBeInTheDocument()
  })

  it("Tentar novamente chama listQueries de novo e recupera a tabela", async () => {
    mockedApi.listQueries.mockRejectedValueOnce(new Error("503"))
    render(<QueriesPage />)
    await screen.findByText("503")

    mockedApi.listQueries.mockResolvedValueOnce([
      { id: 1, title: "Logins suspeitos", statement: "SELECT 1", table: "auth" },
    ])
    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    await waitFor(() => expect(screen.getAllByText("Logins suspeitos").length).toBeGreaterThan(0))
    expect(screen.queryByText("503")).not.toBeInTheDocument()
  })
})
