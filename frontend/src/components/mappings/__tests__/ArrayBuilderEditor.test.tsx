/**
 * Testes de ArrayBuilderEditor + ArrayBuilderItemRow
 * Cobre: render com items vazios, adicionar item, editar campos,
 * toggle de explode/skip_null, reorder, delete, dedup_by parse,
 * estado indeterminate de skip_null.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { ArrayBuilderEditor } from "@/components/mappings/ArrayBuilderEditor"
import type { ArrayBuilderRule } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const EMPTY_RULE: ArrayBuilderRule = {
  target: "normalized.observables",
  kind: "array_builder",
  items: [],
  skip_null: true,
}

const RULE_WITH_ITEMS: ArrayBuilderRule = {
  target: "normalized.observables",
  kind: "array_builder",
  items: [
    { name: "src_ip", type: "IP Address", type_id: 2, source: "data.clientIp" },
    { name: "email_to", type: "Email Address", type_id: 5, source: "data.recipients", explode: true },
    { name: "file_hash", type: "Hash", type_id: 8, source: "data.hash", skip_null: false },
  ],
  skip_null: true,
  dedup_by: ["value"],
}

function renderEditor(
  rule: ArrayBuilderRule = EMPTY_RULE,
  onChange = vi.fn(),
) {
  return render(
    <ArrayBuilderEditor rule={rule} index={0} onChange={onChange} />,
  )
}

// ── Render padrão ─────────────────────────────────────────────────────────────

describe("ArrayBuilderEditor — render padrão", () => {
  it("renderiza o editor com items vazios", () => {
    renderEditor()
    expect(screen.getByTestId("array-builder-editor")).toBeInTheDocument()
    expect(screen.getByTestId("array-builder-empty-items")).toBeInTheDocument()
  })

  it("exibe o botão '+ Adicionar item'", () => {
    renderEditor()
    expect(screen.getByTestId("add-array-builder-item")).toBeInTheDocument()
  })

  it("renderiza campo target com valor correto", () => {
    renderEditor()
    const input = screen.getByDisplayValue("normalized.observables")
    expect(input).toBeInTheDocument()
  })

  it("skip_null checkbox renderiza marcado por padrão (true)", () => {
    renderEditor()
    const checkbox = screen.getByLabelText(/skip_null/i)
    expect(checkbox).toBeChecked()
  })

  it("dedup_by começa vazio quando não definido", () => {
    renderEditor()
    // input de dedup_by está presente mas vazio
    const input = screen.getByLabelText(/dedup_by/i) as HTMLInputElement
    expect(input.value).toBe("")
  })
})

// ── Adicionar item ─────────────────────────────────────────────────────────────

describe("ArrayBuilderEditor — adicionar item", () => {
  it("clicar '+ Adicionar item' chama onChange com novo item de valores default", () => {
    const onChange = vi.fn()
    renderEditor(EMPTY_RULE, onChange)

    fireEvent.click(screen.getByTestId("add-array-builder-item"))

    expect(onChange).toHaveBeenCalledTimes(1)
    const [calledIndex, updatedRule] = onChange.mock.calls[0]
    expect(calledIndex).toBe(0)
    expect(updatedRule.items).toHaveLength(1)
    expect(updatedRule.items[0]).toEqual({
      name: "",
      type: "",
      type_id: 0,
      source: "",
    })
  })

  it("adicionar dois itens produz array com 2 elementos", () => {
    const onChange = vi.fn()
    const { rerender } = renderEditor(EMPTY_RULE, onChange)

    fireEvent.click(screen.getByTestId("add-array-builder-item"))
    const ruleAfterFirst = onChange.mock.calls[0][1] as ArrayBuilderRule

    rerender(<ArrayBuilderEditor rule={ruleAfterFirst} index={0} onChange={onChange} />)
    fireEvent.click(screen.getByTestId("add-array-builder-item"))

    const ruleAfterSecond = onChange.mock.calls[1][1] as ArrayBuilderRule
    expect(ruleAfterSecond.items).toHaveLength(2)
  })
})

// ── Editar campos de item ──────────────────────────────────────────────────────

describe("ArrayBuilderEditor — editar campos de item", () => {
  it("editar campo name propaga mudança para onChange", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const nameInputs = screen.getAllByLabelText(/Item 1: name/i)
    fireEvent.change(nameInputs[0], { target: { value: "dst_ip" } })

    expect(onChange).toHaveBeenCalledTimes(1)
    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].name).toBe("dst_ip")
  })

  it("editar campo type propaga mudança", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const typeInputs = screen.getAllByLabelText(/Item 1: type$/i)
    fireEvent.change(typeInputs[0], { target: { value: "URL" } })

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].type).toBe("URL")
  })

  it("editar campo type_id propaga mudança como número", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const typeIdInputs = screen.getAllByLabelText(/Item 1: type_id/i)
    fireEvent.change(typeIdInputs[0], { target: { value: "99" } })

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].type_id).toBe(99)
  })

  it("editar campo source propaga mudança", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const sourceInputs = screen.getAllByLabelText(/Item 1: source/i)
    fireEvent.change(sourceInputs[0], { target: { value: "data.ip" } })

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].source).toBe("data.ip")
  })
})

// ── Toggle explode / skip_null ─────────────────────────────────────────────────

describe("ArrayBuilderEditor — toggle explode e skip_null", () => {
  it("toggle explode muda o item correto", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    // Item 1 (src_ip) não tem explode — ativar
    const explodeCheckboxes = screen.getAllByLabelText(/Item 1: explode/i)
    expect(explodeCheckboxes[0]).not.toBeChecked()

    fireEvent.click(explodeCheckboxes[0])

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].explode).toBe(true)
    // Outros items não foram alterados
    expect(updatedRule.items[1].explode).toBe(true) // já era true
  })

  it("item com skip_null=false tem checkbox desmarcado", () => {
    renderEditor(RULE_WITH_ITEMS)

    const skipNullBoxes = screen.getAllByLabelText(/Item 3: skip_null/i)
    expect(skipNullBoxes[0]).not.toBeChecked()
  })

  it("item sem skip_null (undefined) tem checkbox em estado indeterminate", () => {
    renderEditor(RULE_WITH_ITEMS)

    // Item 1 (src_ip) não tem skip_null definido — deve ser indeterminate
    const skipNullBoxes = screen.getAllByLabelText(/Item 1: skip_null/i)
    expect((skipNullBoxes[0] as HTMLInputElement).indeterminate).toBe(true)
  })

  it("interagir com skip_null indeterminate retira o override e emite valor negado", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    // Item 1 sem skip_null próprio → indeterminate; ruleSkipNull=true
    // Primeira interação: define skip_null = !true = false
    const skipNullBoxes = screen.getAllByLabelText(/Item 1: skip_null/i)
    fireEvent.click(skipNullBoxes[0])

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].skip_null).toBe(false)
  })
})

// ── Reorder ───────────────────────────────────────────────────────────────────

describe("ArrayBuilderEditor — reorder", () => {
  it("mover item 1 para baixo (↓) troca com item 2", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const downButtons = screen.getAllByLabelText(/Mover item 1 para baixo/i)
    fireEvent.click(downButtons[0])

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].name).toBe("email_to")
    expect(updatedRule.items[1].name).toBe("src_ip")
  })

  it("mover item 2 para cima (↑) troca com item 1", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const upButtons = screen.getAllByLabelText(/Mover item 2 para cima/i)
    fireEvent.click(upButtons[0])

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items[0].name).toBe("email_to")
    expect(updatedRule.items[1].name).toBe("src_ip")
  })

  it("botão ↑ do primeiro item está desabilitado", () => {
    renderEditor(RULE_WITH_ITEMS)
    const upButtons = screen.getAllByLabelText(/Mover item 1 para cima/i)
    expect(upButtons[0]).toBeDisabled()
  })

  it("botão ↓ do último item está desabilitado", () => {
    renderEditor(RULE_WITH_ITEMS)
    const downButtons = screen.getAllByLabelText(/Mover item 3 para baixo/i)
    expect(downButtons[0]).toBeDisabled()
  })
})

// ── Delete ────────────────────────────────────────────────────────────────────

describe("ArrayBuilderEditor — delete", () => {
  it("deletar item 2 remove o item correto", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const deleteButtons = screen.getAllByLabelText(/Remover item 2/i)
    fireEvent.click(deleteButtons[0])

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items).toHaveLength(2)
    expect(updatedRule.items.map((i) => i.name)).toEqual(["src_ip", "file_hash"])
  })

  it("deletar o único item resulta em array vazio", () => {
    const onChange = vi.fn()
    const singleItem: ArrayBuilderRule = {
      ...EMPTY_RULE,
      items: [{ name: "x", type: "T", type_id: 1, source: "s" }],
    }
    renderEditor(singleItem, onChange)

    const deleteButtons = screen.getAllByLabelText(/Remover item 1/i)
    fireEvent.click(deleteButtons[0])

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.items).toHaveLength(0)
  })
})

// ── dedup_by ──────────────────────────────────────────────────────────────────

describe("ArrayBuilderEditor — dedup_by", () => {
  it("renderiza o valor de dedup_by como string separada por vírgula", () => {
    renderEditor(RULE_WITH_ITEMS)
    const input = screen.getByLabelText(/dedup_by/i) as HTMLInputElement
    expect(input.value).toBe("value")
  })

  it("digitar 'value,type' chama onChange com array ['value', 'type']", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const input = screen.getByLabelText(/dedup_by/i)
    fireEvent.change(input, { target: { value: "value, type" } })

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.dedup_by).toEqual(["value", "type"])
  })

  it("limpar o campo dedup_by resulta em dedup_by=undefined", () => {
    const onChange = vi.fn()
    renderEditor(RULE_WITH_ITEMS, onChange)

    const input = screen.getByLabelText(/dedup_by/i)
    fireEvent.change(input, { target: { value: "" } })

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.dedup_by).toBeUndefined()
  })

  it("exibe texto de ajuda sobre o campo dedup_by", () => {
    renderEditor()
    expect(
      screen.getByText(/Lista de campos do observable/i),
    ).toBeInTheDocument()
  })
})

// ── Campos de nível de regra ──────────────────────────────────────────────────

describe("ArrayBuilderEditor — campos de nível de regra", () => {
  it("editar target chama onChange com target atualizado", () => {
    const onChange = vi.fn()
    renderEditor(EMPTY_RULE, onChange)

    const targetInput = screen.getByDisplayValue("normalized.observables")
    fireEvent.change(targetInput, { target: { value: "normalized.network.observables" } })

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.target).toBe("normalized.network.observables")
  })

  it("toggle skip_null no nível da regra propaga corretamente", () => {
    const onChange = vi.fn()
    renderEditor(EMPTY_RULE, onChange)

    const skipNullCheckbox = screen.getByLabelText(/skip_null — omitir/i)
    fireEvent.click(skipNullCheckbox)

    const updatedRule = onChange.mock.calls[0][1] as ArrayBuilderRule
    expect(updatedRule.skip_null).toBe(false)
  })
})
