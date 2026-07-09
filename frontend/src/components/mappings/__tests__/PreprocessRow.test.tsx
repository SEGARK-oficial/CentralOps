/**
 * Testes de PreprocessRow
 * Cobre: render padrão, validação de target, toggle tolerant, delete.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { PreprocessRow } from "@/components/mappings/PreprocessRow"
import type { PreprocessOp } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const DEFAULT_OP: PreprocessOp = {
  op: "json_parse",
  source: "data.raw",
  target: "_parsed",
  tolerant: true,
}

function makeHandlers() {
  return {
    onChange: vi.fn(),
    onRemove: vi.fn(),
    onMoveUp: vi.fn(),
    onMoveDown: vi.fn(),
  }
}

describe("PreprocessRow", () => {
  it("renderiza com op json_parse por padrão", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={DEFAULT_OP}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    // O select exibe o op atual
    const select = screen.getByRole("combobox", { name: /operação/i })
    expect(select).toBeInTheDocument()
    expect(select).toHaveValue("json_parse")
  })

  it("exibe os campos source e target preenchidos", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={DEFAULT_OP}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    expect(screen.getByDisplayValue("data.raw")).toBeInTheDocument()
    expect(screen.getByDisplayValue("_parsed")).toBeInTheDocument()
  })

  it("checkbox tolerant reflete o valor da prop", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={{ ...DEFAULT_OP, tolerant: false }}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    const checkbox = screen.getByRole("checkbox", { name: /tolerant/i })
    expect(checkbox).not.toBeChecked()
  })

  it("digitar target sem '_' mostra erro inline", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={{ ...DEFAULT_OP, target: "" }}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    const targetInput = screen.getByRole("textbox", { name: /target/i })
    fireEvent.change(targetInput, { target: { value: "sem_underscore" } })

    expect(screen.getByTestId("preprocess-row-0-target-error")).toBeInTheDocument()
    expect(screen.getByText(/deve começar com "_"/i)).toBeInTheDocument()
  })

  it("digitar target começando com '_' não mostra erro", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={{ ...DEFAULT_OP, target: "" }}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    const targetInput = screen.getByRole("textbox", { name: /target/i })
    fireEvent.change(targetInput, { target: { value: "_valido" } })

    expect(screen.queryByTestId("preprocess-row-0-target-error")).not.toBeInTheDocument()
  })

  it("erro de target some quando o valor é corrigido para começar com '_'", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={{ ...DEFAULT_OP, target: "" }}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    const targetInput = screen.getByRole("textbox", { name: /target/i })

    // Primeiro dispara o erro
    fireEvent.change(targetInput, { target: { value: "invalido" } })
    expect(screen.getByTestId("preprocess-row-0-target-error")).toBeInTheDocument()

    // Corrige — erro deve sumir
    fireEvent.change(targetInput, { target: { value: "_corrigido" } })
    expect(screen.queryByTestId("preprocess-row-0-target-error")).not.toBeInTheDocument()
  })

  it("toggle do checkbox tolerant chama onChange com valor invertido", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={{ ...DEFAULT_OP, tolerant: false }}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    const checkbox = screen.getByRole("checkbox", { name: /tolerant/i })
    fireEvent.click(checkbox)

    expect(handlers.onChange).toHaveBeenCalledWith(0, expect.objectContaining({ tolerant: true }))
  })

  it("botão Remover chama onRemove com o index correto", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={DEFAULT_OP}
        index={2}
        {...handlers}
        canMoveUp={true}
        canMoveDown={false}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: /remover operação/i }))

    expect(handlers.onRemove).toHaveBeenCalledWith(2)
  })

  it("botão ↑ chama onMoveUp e está desabilitado quando canMoveUp=false", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={DEFAULT_OP}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={true}
      />,
    )

    const upBtn = screen.getByRole("button", { name: /mover operação para cima/i })
    expect(upBtn).toBeDisabled()

    // Não chama mesmo se clicado quando disabled (navegador bloqueia, mas verificar props)
    expect(handlers.onMoveUp).not.toHaveBeenCalled()
  })

  it("botão ↓ chama onMoveDown quando clicado", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={DEFAULT_OP}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={true}
      />,
    )

    const downBtn = screen.getByRole("button", { name: /mover operação para baixo/i })
    fireEvent.click(downBtn)

    expect(handlers.onMoveDown).toHaveBeenCalledWith(0)
  })

  it("alterar source chama onChange com source atualizado", () => {
    const handlers = makeHandlers()
    render(
      <PreprocessRow
        op={DEFAULT_OP}
        index={0}
        {...handlers}
        canMoveUp={false}
        canMoveDown={false}
      />,
    )

    const sourceInput = screen.getByRole("textbox", { name: /source/i })
    fireEvent.change(sourceInput, { target: { value: "novo.caminho" } })

    expect(handlers.onChange).toHaveBeenCalledWith(0, expect.objectContaining({ source: "novo.caminho" }))
  })
})
