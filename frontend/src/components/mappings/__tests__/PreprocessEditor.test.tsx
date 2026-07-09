/**
 * Testes de PreprocessEditor
 * Cobre: lista vazia, adição de op, remoção, colapso.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { PreprocessEditor } from "@/components/mappings/PreprocessEditor"
import type { PreprocessOp } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const OP: PreprocessOp = {
  op: "json_parse",
  source: "data.raw",
  target: "_parsed",
  tolerant: true,
}

describe("PreprocessEditor", () => {
  it("lista vazia exibe apenas o botão de adicionar", () => {
    render(
      <PreprocessEditor
        ops={[]}
        expanded={false}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    expect(screen.getByTestId("preprocess-add-button")).toBeInTheDocument()
    expect(screen.queryByTestId("preprocess-list")).not.toBeInTheDocument()
  })

  it("seção está colapsada por default (expanded=false) — body não renderiza", () => {
    render(
      <PreprocessEditor
        ops={[]}
        expanded={false}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    expect(screen.queryByTestId("preprocess-list")).not.toBeInTheDocument()
  })

  it("expandir a seção revela o body (preprocess-list)", () => {
    render(
      <PreprocessEditor
        ops={[]}
        expanded={true}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    expect(screen.getByTestId("preprocess-list")).toBeInTheDocument()
  })

  it("clicar no toggle chama onToggleExpand", () => {
    const onToggle = vi.fn()
    render(
      <PreprocessEditor
        ops={[]}
        expanded={false}
        onToggleExpand={onToggle}
        onChange={() => {}}
      />,
    )

    fireEvent.click(screen.getByTestId("preprocess-toggle"))
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it("clicar em '+ Adicionar' chama onChange com uma op nova", () => {
    const onChange = vi.fn()
    render(
      <PreprocessEditor
        ops={[]}
        expanded={false}
        onToggleExpand={() => {}}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByTestId("preprocess-add-button"))

    expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ op: "json_parse", tolerant: true }),
    ])
  })

  it("com 1 op, renderiza 1 PreprocessRow quando expanded=true", () => {
    render(
      <PreprocessEditor
        ops={[OP]}
        expanded={true}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    expect(screen.getByTestId("preprocess-row-0")).toBeInTheDocument()
  })

  it("com 2 ops, renderiza 2 PreprocessRow quando expanded=true", () => {
    const ops: PreprocessOp[] = [
      OP,
      { op: "json_parse", source: "other.field", target: "_other", tolerant: false },
    ]

    render(
      <PreprocessEditor
        ops={ops}
        expanded={true}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    expect(screen.getByTestId("preprocess-row-0")).toBeInTheDocument()
    expect(screen.getByTestId("preprocess-row-1")).toBeInTheDocument()
  })

  it("remover a única op chama onChange com lista vazia", () => {
    const onChange = vi.fn()
    render(
      <PreprocessEditor
        ops={[OP]}
        expanded={true}
        onToggleExpand={() => {}}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: /remover operação/i }))

    expect(onChange).toHaveBeenCalledWith([])
  })

  it("readOnly=true não exibe botão de adicionar", () => {
    render(
      <PreprocessEditor
        ops={[]}
        expanded={false}
        onToggleExpand={() => {}}
        onChange={() => {}}
        readOnly
      />,
    )

    expect(screen.queryByTestId("preprocess-add-button")).not.toBeInTheDocument()
  })

  it("readOnly=true com ops mostra view compacta sem botão Remover", () => {
    render(
      <PreprocessEditor
        ops={[OP]}
        expanded={true}
        onToggleExpand={() => {}}
        onChange={() => {}}
        readOnly
      />,
    )

    // Exibe o op mas sem botões de edição
    expect(screen.queryByRole("button", { name: /remover operação/i })).not.toBeInTheDocument()
    // Texto do op visível
    expect(screen.getByText("json_parse")).toBeInTheDocument()
  })

  it("exibe contagem de operações no cabeçalho quando há ops", () => {
    const ops: PreprocessOp[] = [OP, OP]
    render(
      <PreprocessEditor
        ops={ops}
        expanded={false}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    expect(screen.getByText(/2 operações/)).toBeInTheDocument()
  })

  it("cabeçalho tem role=region com aria-labelledby", () => {
    render(
      <PreprocessEditor
        ops={[]}
        expanded={false}
        onToggleExpand={() => {}}
        onChange={() => {}}
      />,
    )

    const region = screen.getByTestId("preprocess-editor")
    expect(region).toHaveAttribute("role", "region")
    expect(region).toHaveAttribute("aria-labelledby")
  })
})
