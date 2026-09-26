import { render, screen } from "@testing-library/react"
import { Badge } from "@/components/ui/Badge/Badge"

describe("Badge", () => {
  it("renderiza o conteúdo", () => {
    render(<Badge>Ativo</Badge>)
    expect(screen.getByText("Ativo")).toBeInTheDocument()
  })

  // A11Y-34: `aria-label` num `<span>` sem role é ignorado por leitores de
  // tela (role "generic" não suporta nome via autor). `role="img"` dá ao
  // aria-label um lugar pra valer — só quando o chamador passa um.
  it("aria-label sem role explícito vira role=img (A11Y-34)", () => {
    render(<Badge aria-label="XDR — 42 de 100 usados">XDR</Badge>)
    expect(screen.getByRole("img", { name: "XDR — 42 de 100 usados" })).toBeInTheDocument()
  })

  it("sem aria-label, não ganha role nenhum (comportamento antigo preservado)", () => {
    render(<Badge>Ativo</Badge>)
    const badge = screen.getByText("Ativo")
    expect(badge).not.toHaveAttribute("role")
  })

  it("respeita um role explícito em vez de forçar img", () => {
    render(<Badge role="status" aria-label="Processando">...</Badge>)
    expect(screen.getByRole("status", { name: "Processando" })).toBeInTheDocument()
  })
})
