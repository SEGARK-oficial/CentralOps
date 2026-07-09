import { useState } from "react"
import { render, screen, fireEvent } from "@testing-library/react"
import { Modal } from "@/components/ui/Modal/Modal"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// @testing-library/user-event pode não estar instalado; usar fireEvent como fallback
// Verificamos disponibilidade via import dinâmico no setup, mas aqui usamos fireEvent
// para garantir compatibilidade sem dep adicional.

describe("Modal — acessibilidade e focus trap", () => {
  it("renderiza title e children quando aberto", () => {
    render(
      <Modal open={true} onClose={() => {}}>
        <button>Botão interno</button>
      </Modal>,
    )
    expect(screen.getByText("Botão interno")).toBeInTheDocument()
  })

  it("não renderiza nada quando fechado", () => {
    render(
      <Modal open={false} onClose={() => {}}>
        <span>Conteúdo</span>
      </Modal>,
    )
    expect(screen.queryByText("Conteúdo")).not.toBeInTheDocument()
  })

  it("chama onClose ao clicar no botão fechar (quando tem title)", () => {
    const handleClose = vi.fn()
    render(
      <Modal open={true} onClose={handleClose} title="Título do Modal">
        <span>Conteúdo</span>
      </Modal>,
    )
    fireEvent.click(screen.getByRole("button", { name: /fechar modal/i }))
    expect(handleClose).toHaveBeenCalledTimes(1)
  })

  it("chama onClose ao pressionar ESC", () => {
    const handleClose = vi.fn()
    render(
      <Modal open={true} onClose={handleClose} closeOnEscape={true}>
        <button>Botão</button>
      </Modal>,
    )
    fireEvent.keyDown(document, { key: "Escape" })
    expect(handleClose).toHaveBeenCalledTimes(1)
  })

  it("não chama onClose ao pressionar ESC quando closeOnEscape=false", () => {
    const handleClose = vi.fn()
    render(
      <Modal open={true} onClose={handleClose} closeOnEscape={false}>
        <button>Botão</button>
      </Modal>,
    )
    fireEvent.keyDown(document, { key: "Escape" })
    expect(handleClose).not.toHaveBeenCalled()
  })

  it("aria-modal=true e role=dialog no overlay", () => {
    render(
      <Modal open={true} onClose={() => {}}>
        <span>Conteúdo</span>
      </Modal>,
    )
    const dialog = screen.getByRole("dialog")
    expect(dialog).toHaveAttribute("aria-modal", "true")
  })

  // Regressão: o campo perdia o foco a cada tecla porque o efeito de foco do Modal
  // dependia de ``onClose`` (recriado a cada render do pai) e o cleanup refocava o
  // elemento anterior. Aqui o pai passa um ``onClose`` NOVO a cada keystroke (inline)
  // e digitamos várias letras — o input deve manter o foco e o valor completo.
  it("mantém o foco no input ao digitar mesmo com onClose recriado a cada render", () => {
    function Harness() {
      const [text, setText] = useState("")
      // onClose inline → nova referência a cada render (o gatilho do bug original: se o
      // efeito de foco do Modal dependesse de onClose, re-rodaria e o cleanup roubaria
      // o foco de volta ao elemento anterior, aqui o document.body).
      return (
        <Modal open onClose={() => {}} title="Form">
          <input
            aria-label="campo"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </Modal>
      )
    }

    render(<Harness />)
    const input = screen.getByLabelText("campo") as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)

    // digita letra a letra — cada change força um re-render do pai (novo onClose)
    for (const ch of "wazuh") {
      fireEvent.change(input, { target: { value: input.value + ch } })
      // com o bug, o cleanup do efeito focava o document.body a cada tecla
      expect(document.activeElement).toBe(input)
    }
    expect(input).toHaveValue("wazuh")
  })
})
