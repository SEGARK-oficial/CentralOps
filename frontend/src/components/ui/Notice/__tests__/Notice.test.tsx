import { render, screen } from "@testing-library/react"
import { Notice } from "@/components/ui/Notice/Notice"

/**
 * R2-8.2: TODA variante nasce polite/status por padrão — mesmo danger/warning.
 * `assertive`/`role=alert` virou opt-in explícito (`live="assertive"`), porque
 * a imensa maioria dos usos deste componente é um banner montado ESTATICAMENTE
 * junto com a página (erro de carga, config desatualizada), não uma reação
 * imediata a uma ação do usuário — interromper o leitor de tela sem ação
 * nenhuma do operador para justificar é o defeito que este teste trava.
 */
describe("Notice — role/aria-live por variante (R2-8.2)", () => {
  it("danger é polite/status por padrão (banner estático, sem ação do usuário)", () => {
    render(<Notice variant="danger">falhou</Notice>)
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    const el = screen.getByRole("status")
    expect(el).toHaveAttribute("aria-live", "polite")
  })

  it("warning é polite/status por padrão", () => {
    render(<Notice variant="warning">atenção</Notice>)
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    const el = screen.getByRole("status")
    expect(el).toHaveAttribute("aria-live", "polite")
  })

  it("info/success são polite/status por padrão", () => {
    render(<Notice variant="info">ok</Notice>)
    const el = screen.getByRole("status")
    expect(el).toHaveAttribute("aria-live", "polite")
  })

  it("live='assertive' promove danger para alert/assertive (opt-in explícito — erro de submit)", () => {
    render(
      <Notice variant="danger" live="assertive">
        falha ao salvar
      </Notice>,
    )
    expect(screen.queryByRole("status")).not.toBeInTheDocument()
    const el = screen.getByRole("alert")
    expect(el).toHaveAttribute("aria-live", "assertive")
  })

  it("live='assertive' promove warning para alert/assertive", () => {
    render(
      <Notice variant="warning" live="assertive">
        atenção urgente
      </Notice>,
    )
    const el = screen.getByRole("alert")
    expect(el).toHaveAttribute("aria-live", "assertive")
  })
})
