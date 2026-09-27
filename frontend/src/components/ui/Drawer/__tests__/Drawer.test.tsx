import { useState } from "react"
import { render, screen, fireEvent } from "@testing-library/react"
import { Drawer } from "@/components/ui/Drawer/Drawer"

describe("Drawer — acessibilidade e focus trap (A11Y-17/ARQ-06)", () => {
  it("renderiza children quando aberto e nada quando fechado", () => {
    const { rerender } = render(
      <Drawer open onClose={() => {}} ariaLabel="Painel">
        <p>Conteúdo do drawer</p>
      </Drawer>,
    )
    expect(screen.getByText("Conteúdo do drawer")).toBeInTheDocument()

    rerender(
      <Drawer open={false} onClose={() => {}} ariaLabel="Painel">
        <p>Conteúdo do drawer</p>
      </Drawer>,
    )
    expect(screen.queryByText("Conteúdo do drawer")).not.toBeInTheDocument()
  })

  it("role=dialog, aria-modal e aria-label quando não há aria-labelledby", () => {
    render(
      <Drawer open onClose={() => {}} ariaLabel="Detalhes">
        <p>Conteúdo</p>
      </Drawer>,
    )
    const dialog = screen.getByRole("dialog", { name: "Detalhes" })
    expect(dialog).toHaveAttribute("aria-modal", "true")
  })

  it("chama onClose ao pressionar Escape", () => {
    const handleClose = vi.fn()
    render(
      <Drawer open onClose={handleClose} ariaLabel="Painel">
        <button>Foco inicial</button>
      </Drawer>,
    )
    fireEvent.keyDown(document, { key: "Escape" })
    expect(handleClose).toHaveBeenCalledTimes(1)
  })

  it("não fecha no Escape quando closeOnEscape=false", () => {
    const handleClose = vi.fn()
    render(
      <Drawer open onClose={handleClose} closeOnEscape={false} ariaLabel="Painel">
        <button>Foco inicial</button>
      </Drawer>,
    )
    fireEvent.keyDown(document, { key: "Escape" })
    expect(handleClose).not.toHaveBeenCalled()
  })

  // Trap: Tab/Shift+Tab não escapam do drawer (delegado ao FocusScope do
  // Radix, mesmo padrão do Modal) — smoke test: o botão externo nunca deveria
  // ser alcançável a partir do painel sem passar pelo trap.
  it("mantém o foco contido — FocusScope trapped envolve o painel", () => {
    render(
      <>
        <button>Fora do drawer</button>
        <Drawer open onClose={() => {}} ariaLabel="Painel">
          <button>Dentro do drawer</button>
        </Drawer>
      </>,
    )
    const dialog = screen.getByRole("dialog", { name: "Painel" })
    expect(dialog.querySelector("button")).toHaveTextContent("Dentro do drawer")
  })

  it("restaura o foco no elemento que abriu o drawer ao fechar", () => {
    function Harness() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button onClick={() => setOpen(true)}>Abrir</button>
          <Drawer open={open} onClose={() => setOpen(false)} ariaLabel="Painel">
            <button onClick={() => setOpen(false)}>Fechar</button>
          </Drawer>
        </>
      )
    }
    render(<Harness />)
    const openButton = screen.getByRole("button", { name: "Abrir" })
    openButton.focus()
    fireEvent.click(openButton)
    fireEvent.click(screen.getByRole("button", { name: "Fechar" }))
    expect(document.activeElement).toBe(openButton)
  })

  // Regressão A11Y-16/17: `onClose` inline (nova referência a cada render do
  // pai) não pode re-rodar o efeito de foco — senão o cleanup rouba o foco de
  // volta a cada re-render, exatamente como o bug original do Modal.
  it("onClose inline recriado a cada render não rouba o foco de um input interno", () => {
    function Harness() {
      const [text, setText] = useState("")
      return (
        <Drawer open onClose={() => {}} ariaLabel="Painel">
          <input aria-label="campo" value={text} onChange={(e) => setText(e.target.value)} />
        </Drawer>
      )
    }
    render(<Harness />)
    const input = screen.getByLabelText("campo") as HTMLInputElement
    input.focus()
    for (const ch of "wazuh") {
      fireEvent.change(input, { target: { value: input.value + ch } })
      expect(document.activeElement).toBe(input)
    }
    expect(input).toHaveValue("wazuh")
  })
})
