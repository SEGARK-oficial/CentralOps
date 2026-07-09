/**
 * Testes de BulkActionBar — render condicional, contagem, slot, a11y.
 */

import { fireEvent, render, screen } from "@testing-library/react"
import { BulkActionBar } from "@/components/ui/BulkActionBar/BulkActionBar"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

describe("BulkActionBar — render condicional", () => {
  it("não renderiza nada quando count === 0", () => {
    const { container } = render(<BulkActionBar count={0} onClear={() => {}} />)
    expect(container.firstChild).toBeNull()
  })

  it("não renderiza quando count negativo (defensivo)", () => {
    const { container } = render(<BulkActionBar count={-1} onClear={() => {}} />)
    expect(container.firstChild).toBeNull()
  })

  it("renderiza quando count >= 1", () => {
    render(<BulkActionBar count={1} onClear={() => {}} />)
    expect(screen.getByTestId("bulk-action-bar")).toBeInTheDocument()
  })
})

describe("BulkActionBar — contagem singular/plural sem contextLabel", () => {
  it("'1 selecionado' (singular)", () => {
    render(<BulkActionBar count={1} onClear={() => {}} />)
    expect(screen.getByText(/^1 selecionado$/)).toBeInTheDocument()
  })

  it("'5 selecionados' (plural)", () => {
    render(<BulkActionBar count={5} onClear={() => {}} />)
    expect(screen.getByText(/^5 selecionados$/)).toBeInTheDocument()
  })
})

describe("BulkActionBar — contagem com contextLabel", () => {
  it("usa o contextLabel passado", () => {
    render(<BulkActionBar count={3} onClear={() => {}} contextLabel="tenant(s)" />)
    expect(screen.getByText("3 tenant(s) selecionado(s)")).toBeInTheDocument()
  })

  it("contextLabel='campos' renderiza '{N} campos selecionado(s)'", () => {
    render(<BulkActionBar count={2} onClear={() => {}} contextLabel="campos" />)
    expect(screen.getByText("2 campos selecionado(s)")).toBeInTheDocument()
  })
})

describe("BulkActionBar — onClear", () => {
  it("clicar em 'Limpar seleção' chama onClear", () => {
    const onClear = vi.fn()
    render(<BulkActionBar count={1} onClear={onClear} />)
    fireEvent.click(screen.getByTestId("bulk-action-bar-clear"))
    expect(onClear).toHaveBeenCalledTimes(1)
  })

  it("botão limpar tem aria-label='Limpar seleção'", () => {
    render(<BulkActionBar count={1} onClear={() => {}} />)
    expect(screen.getByTestId("bulk-action-bar-clear")).toHaveAttribute(
      "aria-label",
      "Limpar seleção",
    )
  })
})

describe("BulkActionBar — slot children", () => {
  it("renderiza children passados como slot", () => {
    render(
      <BulkActionBar count={1} onClear={() => {}}>
        <button type="button" data-testid="approve-btn">
          Aprovar
        </button>
        <button type="button" data-testid="delete-btn">
          Excluir
        </button>
      </BulkActionBar>,
    )
    expect(screen.getByTestId("approve-btn")).toBeInTheDocument()
    expect(screen.getByTestId("delete-btn")).toBeInTheDocument()
  })

  it("children podem ser null sem quebrar", () => {
    render(<BulkActionBar count={1} onClear={() => {}}>{null}</BulkActionBar>)
    expect(screen.getByTestId("bulk-action-bar")).toBeInTheDocument()
  })
})

describe("BulkActionBar — acessibilidade", () => {
  it("region tem aria-label 'Ações em massa'", () => {
    render(<BulkActionBar count={1} onClear={() => {}} />)
    expect(screen.getByRole("region", { name: /ações em massa/i })).toBeInTheDocument()
  })

  it("contador tem aria-live='polite'", () => {
    render(<BulkActionBar count={2} onClear={() => {}} />)
    const counter = screen.getByTestId("bulk-action-bar-count")
    expect(counter).toHaveAttribute("aria-live", "polite")
  })
})

describe("BulkActionBar — data-testid customizável", () => {
  it("permite override do test-id", () => {
    render(
      <BulkActionBar count={1} onClear={() => {}} data-testid="drift-bulk-bar" />,
    )
    expect(screen.getByTestId("drift-bulk-bar")).toBeInTheDocument()
    expect(screen.getByTestId("drift-bulk-bar-clear")).toBeInTheDocument()
    expect(screen.getByTestId("drift-bulk-bar-count")).toBeInTheDocument()
  })
})
