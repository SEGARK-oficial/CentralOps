import { render, screen } from "@testing-library/react"
import { Notice } from "@/components/ui/Notice/Notice"

describe("Notice — role/aria-live por variante (A11Y-31)", () => {
  it("danger é assertive/alert por padrão", () => {
    render(<Notice variant="danger">falhou</Notice>)
    const el = screen.getByRole("alert")
    expect(el).toHaveAttribute("aria-live", "assertive")
  })

  it("warning é assertive/alert por padrão", () => {
    render(<Notice variant="warning">atenção</Notice>)
    const el = screen.getByRole("alert")
    expect(el).toHaveAttribute("aria-live", "assertive")
  })

  it("info/success são polite/status por padrão", () => {
    render(<Notice variant="info">ok</Notice>)
    const el = screen.getByRole("status")
    expect(el).toHaveAttribute("aria-live", "polite")
  })

  it("live='polite' rebaixa danger/warning para status (role e aria-live juntos)", () => {
    render(<Notice variant="danger" live="polite">aviso recorrente</Notice>)
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    const el = screen.getByRole("status")
    expect(el).toHaveAttribute("aria-live", "polite")
  })
})
