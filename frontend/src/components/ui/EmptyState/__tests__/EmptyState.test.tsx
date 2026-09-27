import { render, screen } from "@testing-library/react"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"

describe("EmptyState", () => {
  it("renderiza título e descrição", () => {
    render(<EmptyState title="Nenhum item" description="Crie o primeiro para começar." />)
    expect(screen.getByText("Nenhum item")).toBeInTheDocument()
    expect(screen.getByText("Crie o primeiro para começar.")).toBeInTheDocument()
  })

  it("renderiza a ação quando fornecida", () => {
    render(<EmptyState title="Vazio" action={<button>Criar</button>} />)
    expect(screen.getByRole("button", { name: "Criar" })).toBeInTheDocument()
  })

  // A11Y-33: default h3 preserva o comportamento atual; headingLevel permite
  // ajustar quando o EmptyState é o único heading da seção.
  it("headingLevel default é h3", () => {
    render(<EmptyState title="Nenhum item" />)
    expect(screen.getByRole("heading", { level: 3, name: "Nenhum item" })).toBeInTheDocument()
  })

  it("headingLevel=2 renderiza h2", () => {
    render(<EmptyState title="Nenhum item" headingLevel={2} />)
    expect(screen.getByRole("heading", { level: 2, name: "Nenhum item" })).toBeInTheDocument()
  })
})
