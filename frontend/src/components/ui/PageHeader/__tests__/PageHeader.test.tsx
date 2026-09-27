import { render, screen } from "@testing-library/react"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { ShieldIcon } from "lucide-react"

describe("PageHeader", () => {
  it("renderiza título, descrição e eyebrow", () => {
    render(<PageHeader title="Integrações" description="Conecte uma fonte" eyebrow="Coleta" />)
    expect(screen.getByRole("heading", { level: 1, name: "Integrações" })).toBeInTheDocument()
    expect(screen.getByText("Conecte uma fonte")).toBeInTheDocument()
    expect(screen.getByText("Coleta")).toBeInTheDocument()
  })

  // A11Y-43: o chip do ícone é decorativo — não deve poluir a árvore de
  // acessibilidade.
  it("o wrapper do ícone é aria-hidden", () => {
    const { container } = render(<PageHeader title="Integrações" icon={<ShieldIcon size={18} />} />)
    const iconWrapper = container.querySelector('[aria-hidden="true"]')
    expect(iconWrapper).not.toBeNull()
    expect(iconWrapper?.querySelector("svg")).not.toBeNull()
  })

  // LAY-25: título longo não deve estourar o layout — min-w-0 no flex item +
  // break-words no h1.
  it("h1 tem break-words para títulos longos", () => {
    render(<PageHeader title="Um título bem comprido para forçar quebra de linha no cabeçalho da página" />)
    const heading = screen.getByRole("heading", { level: 1 })
    expect(heading.className).toContain("break-words")
  })
})
