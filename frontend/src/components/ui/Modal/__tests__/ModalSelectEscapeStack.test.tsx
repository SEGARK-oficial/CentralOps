/**
 * Regressão A11Y-01 / A11Y-02: Select dentro de Modal por teclado + pilha de Escape.
 *
 * A11Y-01: a listbox do Select é portada para `document.body`, fora do
 * `FocusScope trapped` do Modal — o Radix devolve o foco ao gatilho e a
 * seleção por teclado dentro de um Modal não funcionava. Fix:
 * `PortalContainerContext` faz o Select portar para o painel do Modal.
 *
 * A11Y-02: o Escape era ouvido no `document` por CADA Modal aberto — um
 * ConfirmDialog aninhado fechava os dois, e o Escape do Select aberto fechava
 * o Modal por baixo. Fix: pilha de modais (só o topo reage) + o Select
 * consome o próprio Escape (`stopPropagation`/`preventDefault`).
 */
import { useState } from "react"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { Modal } from "@/components/ui/Modal/Modal"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Select } from "@/components/ui/Select/Select"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const OPTIONS = [
  { value: "a", label: "Alfa" },
  { value: "b", label: "Beta" },
  { value: "c", label: "Gama" },
]

describe("Select dentro de Modal — teclado (A11Y-01)", () => {
  it("abrir o Select, ArrowDown, Enter seleciona a opção sem fechar o Modal", async () => {
    const onChange = vi.fn()
    const onModalClose = vi.fn()
    render(
      <Modal open onClose={onModalClose} title="Config">
        <Select options={OPTIONS} aria-label="modo" onChange={onChange} />
      </Modal>,
    )

    fireEvent.click(screen.getByLabelText("modo"))
    const listbox = await screen.findByRole("listbox")
    expect(listbox).toBeInTheDocument()

    // Foco inicial cai na 1ª opção (efeito assíncrono via setTimeout(0)).
    await waitFor(() => {
      expect(document.activeElement).toHaveAttribute("role", "option")
    })
    expect(document.activeElement).toHaveTextContent("Alfa")

    fireEvent.keyDown(document.activeElement as Element, { key: "ArrowDown" })
    expect(document.activeElement).toHaveTextContent("Beta")

    fireEvent.keyDown(document.activeElement as Element, { key: "Enter" })

    expect(onChange).toHaveBeenCalledWith("b")
    // A seleção por teclado dentro do Modal não deve fechá-lo.
    expect(onModalClose).not.toHaveBeenCalled()
    expect(screen.getByText("Config")).toBeInTheDocument()
  })

  it("Escape com o Select aberto fecha só o dropdown, não o Modal", async () => {
    const onModalClose = vi.fn()
    render(
      <Modal open onClose={onModalClose} title="Config">
        <Select options={OPTIONS} aria-label="modo" />
      </Modal>,
    )

    fireEvent.click(screen.getByLabelText("modo"))
    await screen.findByRole("listbox")
    await waitFor(() => expect(document.activeElement).toHaveAttribute("role", "option"))

    fireEvent.keyDown(document.activeElement as Element, { key: "Escape" })

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument()
    expect(onModalClose).not.toHaveBeenCalled()
    // O Modal segue aberto — o foco volta ao gatilho do Select.
    expect(screen.getByText("Config")).toBeInTheDocument()
  })
})

describe("Pilha de Escape entre Modais (A11Y-02)", () => {
  it("ConfirmDialog aninhado: Escape fecha só o de cima, o Modal de baixo continua aberto", () => {
    function Harness() {
      const [modalOpen, setModalOpen] = useState(true)
      const [confirmOpen, setConfirmOpen] = useState(true)
      return (
        <Modal open={modalOpen} onClose={() => setModalOpen(false)} title="Editar item">
          <p>Conteúdo do modal de baixo</p>
          <ConfirmDialog
            open={confirmOpen}
            title="Descartar alterações?"
            description="Essa ação não pode ser desfeita."
            onConfirm={() => setConfirmOpen(false)}
            onClose={() => setConfirmOpen(false)}
          />
        </Modal>
      )
    }

    render(<Harness />)

    expect(screen.getByText("Editar item")).toBeInTheDocument()
    expect(screen.getByText("Descartar alterações?")).toBeInTheDocument()

    fireEvent.keyDown(document, { key: "Escape" })

    // Só o topo (ConfirmDialog) fecha.
    expect(screen.queryByText("Descartar alterações?")).not.toBeInTheDocument()
    expect(screen.getByText("Editar item")).toBeInTheDocument()

    fireEvent.keyDown(document, { key: "Escape" })
    expect(screen.queryByText("Editar item")).not.toBeInTheDocument()
  })
})
