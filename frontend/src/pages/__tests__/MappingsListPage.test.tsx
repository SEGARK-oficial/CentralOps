/**
 * Testes de MappingsListPage
 * Cobre: render padrão, filtros (busca + vendor + event_type + combinados),
 *        clique em Editar navega, empty state, erro state, badge sem versão.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import MappingsListPage from "@/pages/MappingsListPage"
import * as api from "@/services/api"
import type { MappingListItem } from "@/services/api"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listMappings: vi.fn(),
  }
})

const mockedListMappings = vi.mocked(api.listMappings)

const MAPPINGS: MappingListItem[] = [
  {
    id: "map-001",
    vendor: "wazuh",
    event_type: "authentication",
    description: "Mapeamento de autenticação",
    current_version_id: "ver-001",
    rules_count: 5,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-03-15T10:30:00Z",
  },
  {
    id: "map-002",
    vendor: "crowdstrike",
    event_type: "network",
    description: null,
    current_version_id: "ver-002",
    rules_count: 3,
    created_at: "2026-02-01T00:00:00Z",
    updated_at: "2026-04-01T08:00:00Z",
  },
  {
    id: "map-003",
    vendor: "wazuh",
    event_type: "network",
    description: "Rede wazuh",
    current_version_id: null,
    rules_count: null,
    created_at: "2026-03-01T00:00:00Z",
    updated_at: "2026-04-10T12:00:00Z",
  },
]

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/mappings"]}>
      <Routes>
        <Route path="/mappings" element={<MappingsListPage />} />
        <Route path="/mappings/:id" element={<div data-testid="mapping-editor">Editor</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedListMappings.mockResolvedValue(MAPPINGS)
})

describe("MappingsListPage — render padrão", () => {
  it("exibe o título 'Mappings'", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByRole("heading", { name: "Mappings" })).toBeInTheDocument())
  })

  it("exibe o eyebrow 'Normalização'", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Normalização")).toBeInTheDocument())
  })

  it("exibe o data-testid da página", async () => {
    renderPage()
    expect(screen.getByTestId("mappings-list-page")).toBeInTheDocument()
  })

  it("chama listMappings com include_rules_count=true", async () => {
    renderPage()
    await waitFor(() => expect(mockedListMappings).toHaveBeenCalledWith(
      expect.objectContaining({ include_rules_count: true }),
    ))
  })

  it("renderiza linhas da tabela com vendor e event_type", async () => {
    renderPage()
    await waitFor(() => {
      // Pode haver múltiplos elementos com esses textos (tabela + opções do select)
      expect(screen.getAllByText("wazuh").length).toBeGreaterThan(0)
      expect(screen.getAllByText("authentication").length).toBeGreaterThan(0)
      expect(screen.getAllByText("crowdstrike").length).toBeGreaterThan(0)
    })
  })

  it("renderiza descrição quando presente", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Mapeamento de autenticação")).toBeInTheDocument())
  })

  it("renderiza '—' quando descrição é null", async () => {
    renderPage()
    await waitFor(() => {
      // map-002 tem description null
      const dashes = screen.getAllByText("—")
      expect(dashes.length).toBeGreaterThan(0)
    })
  })

  it("renderiza contagem de regras como texto mono para mappings com versão", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText("5")).toBeInTheDocument()
      expect(screen.getByText("3")).toBeInTheDocument()
    })
  })
})

describe("MappingsListPage — badge sem versão", () => {
  it("exibe badge 'sem versão' quando current_version_id é null", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("sem versão")).toBeInTheDocument())
  })
})

describe("MappingsListPage — filtros", () => {
  it("filtro de busca por vendor filtra a lista", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("crowdstrike")).toBeInTheDocument())

    const searchInput = screen.getByTestId("mappings-search")
    fireEvent.change(searchInput, { target: { value: "crowdstrike" } })

    await waitFor(() => {
      expect(screen.getByText("crowdstrike")).toBeInTheDocument()
      expect(screen.queryByText("authentication")).not.toBeInTheDocument()
    })
  })

  it("filtro de busca por descrição filtra a lista", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Rede wazuh")).toBeInTheDocument())

    const searchInput = screen.getByTestId("mappings-search")
    fireEvent.change(searchInput, { target: { value: "Rede wazuh" } })

    await waitFor(() => {
      expect(screen.getByText("Rede wazuh")).toBeInTheDocument()
      expect(screen.queryByText("crowdstrike")).not.toBeInTheDocument()
      expect(screen.queryByText("Mapeamento de autenticação")).not.toBeInTheDocument()
    })
  })

  it("filtro de busca por event_type filtra a lista", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("authentication")).toBeInTheDocument())

    const searchInput = screen.getByTestId("mappings-search")
    fireEvent.change(searchInput, { target: { value: "authentication" } })

    await waitFor(() => {
      expect(screen.getByText("authentication")).toBeInTheDocument()
      expect(screen.queryByText("crowdstrike")).not.toBeInTheDocument()
    })
  })

  it("campo de busca tem aria-label acessível", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByLabelText("Buscar mapping")).toBeInTheDocument())
  })
})

describe("MappingsListPage — navegação ao editar", () => {
  it("botão Editar está presente com data-testid correto", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId("edit-mapping-map-001")).toBeInTheDocument())
  })

  it("clicar em Editar navega para /mappings/:id", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId("edit-mapping-map-001")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("edit-mapping-map-001"))

    await waitFor(() => expect(screen.getByTestId("mapping-editor")).toBeInTheDocument())
  })

  it("botão Editar tem aria-label descritivo", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Editar mapping wazuh/authentication" })).toBeInTheDocument(),
    )
  })
})

describe("MappingsListPage — empty state", () => {
  it("lista vazia (default = só integrações ativas) mostra empty state com 'Mostrar todos'", async () => {
    mockedListMappings.mockResolvedValue([])
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Nenhum mapping para integrações ativas")).toBeInTheDocument(),
    )
    expect(screen.getByTestId("mappings-empty-show-all")).toBeInTheDocument()
  })

  it("exibe mensagem de filtro vazio quando há dados mas filtro não encontra resultado", async () => {
    renderPage()
    // Aguarda o carregamento completar verificando a presença dos botões de editar
    await waitFor(() => expect(screen.getByTestId("edit-mapping-map-001")).toBeInTheDocument())

    const searchInput = screen.getByTestId("mappings-search")
    fireEvent.change(searchInput, { target: { value: "xxxxxxxxxxx" } })

    await waitFor(() =>
      expect(screen.getByText("Nenhum mapping encontrado com os filtros aplicados.")).toBeInTheDocument(),
    )
  })
})

describe("MappingsListPage — estado de erro", () => {
  it("exibe Notice de erro quando fetch falha", async () => {
    mockedListMappings.mockRejectedValue(new Error("Falha de rede"))
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Erro ao carregar mappings")).toBeInTheDocument(),
    )
    expect(screen.getByText("Falha de rede")).toBeInTheDocument()
  })

  it("não exibe spinner depois do erro", async () => {
    mockedListMappings.mockRejectedValue(new Error("Timeout"))
    renderPage()
    await waitFor(() => expect(screen.getByText("Erro ao carregar mappings")).toBeInTheDocument())
    expect(screen.queryByText("Carregando mappings...")).not.toBeInTheDocument()
  })
})

describe("MappingsListPage — loading state", () => {
  it("exibe spinner enquanto carrega", () => {
    // Nunca resolve durante o teste
    mockedListMappings.mockImplementation(() => new Promise(() => {}))
    renderPage()
    expect(screen.getByText("Carregando mappings...")).toBeInTheDocument()
  })
})

describe("MappingsListPage — acessibilidade teclado", () => {
  it("botão Editar é acessível via teclado (é um button, não div)", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByTestId("edit-mapping-map-001")).toBeInTheDocument())
    const btn = screen.getByTestId("edit-mapping-map-001")
    expect(btn.tagName).toBe("BUTTON")
  })

  it("campo de busca é acessível pelo label", async () => {
    renderPage()
    await waitFor(() => {
      const input = screen.getByLabelText("Buscar mapping")
      expect(input).toBeInTheDocument()
      expect(input.tagName).toBe("INPUT")
    })
  })
})
