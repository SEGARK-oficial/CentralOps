/**
 * Button — render padrão, variantes/estados e o `asChild` (A11Y-41): existia
 * mas nunca tinha sido usado no app, com um bug latente de "React.Children.only"
 * assim que combinado com `leftIcon`/`rightIcon`/`loading` (o Slot do Radix só
 * aceita UM filho).
 */
import { MemoryRouter, Link } from "react-router-dom"
import { render, screen, fireEvent } from "@testing-library/react"
import { Button } from "@/components/ui/Button/Button"
import { PlusIcon } from "lucide-react"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

describe("Button — render padrão", () => {
  it("renderiza como <button type='button'> por padrão", () => {
    render(<Button>Salvar</Button>)
    const btn = screen.getByRole("button", { name: "Salvar" })
    expect(btn.tagName).toBe("BUTTON")
    expect(btn).toHaveAttribute("type", "button")
  })

  it("chama onClick", () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick}>Salvar</Button>)
    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it("disabled bloqueia onClick", () => {
    const onClick = vi.fn()
    render(<Button onClick={onClick} disabled>Salvar</Button>)
    const btn = screen.getByRole("button", { name: "Salvar" })
    expect(btn).toBeDisabled()
    fireEvent.click(btn)
    expect(onClick).not.toHaveBeenCalled()
  })

  it("loading desabilita o botão e anuncia via sr-only", () => {
    render(<Button loading>Salvar</Button>)
    const btn = screen.getByRole("button")
    expect(btn).toBeDisabled()
    expect(screen.getByText("Carregando…")).toHaveClass("sr-only")
  })

  it("renderiza leftIcon e rightIcon decorativos (aria-hidden)", () => {
    render(
      <Button leftIcon={<PlusIcon data-testid="left" />} rightIcon={<PlusIcon data-testid="right" />}>
        Criar
      </Button>,
    )
    expect(screen.getByTestId("left").closest("span")).toHaveAttribute("aria-hidden", "true")
    expect(screen.getByTestId("right").closest("span")).toHaveAttribute("aria-hidden", "true")
  })
})

describe("Button — asChild (A11Y-41)", () => {
  it("renderiza como o elemento filho (ex.: Link) em vez de <button>", () => {
    render(
      <MemoryRouter>
        <Button asChild>
          <Link to="/destinations/1">Ver detalhes</Link>
        </Button>
      </MemoryRouter>,
    )
    const link = screen.getByRole("link", { name: "Ver detalhes" })
    expect(link).toHaveAttribute("href", "/destinations/1")
  })

  it("aplica as classes de variante/tamanho no elemento filho", () => {
    render(
      <MemoryRouter>
        <Button asChild variant="outline" size="sm">
          <Link to="/x">Ver</Link>
        </Button>
      </MemoryRouter>,
    )
    const link = screen.getByRole("link", { name: "Ver" })
    expect(link.className).toContain("border")
    expect(link.className).toContain("h-8")
  })

  // Regressão do bug: asChild + leftIcon quebrava com "React.Children.only"
  // porque o Slot recebia 2 filhos (ícone + label) em vez de 1.
  it("asChild + leftIcon NÃO quebra (o ícone entra como filho do Link, não irmão)", () => {
    render(
      <MemoryRouter>
        <Button asChild leftIcon={<PlusIcon data-testid="icon" />}>
          <Link to="/destinations/1">Ver detalhes</Link>
        </Button>
      </MemoryRouter>,
    )
    const link = screen.getByRole("link", { name: "Ver detalhes" })
    expect(screen.getByTestId("icon")).toBeInTheDocument()
    // O ícone deve estar DENTRO do próprio link (é o único nó real no DOM).
    expect(link.contains(screen.getByTestId("icon"))).toBe(true)
  })

  it("asChild + rightIcon + loading também não quebra", () => {
    render(
      <MemoryRouter>
        <Button asChild rightIcon={<PlusIcon data-testid="icon" />} loading>
          <Link to="/x">Ver</Link>
        </Button>
      </MemoryRouter>,
    )
    expect(screen.getByRole("link")).toBeInTheDocument()
  })
})
