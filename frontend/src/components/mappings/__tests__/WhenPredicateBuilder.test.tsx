/**
 * Testes de WhenPredicateBuilder
 * Cobre: todos os operadores, aninhamento (not), validação leve, remoção.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { WhenPredicateBuilder } from "@/components/mappings/WhenPredicateBuilder"
import type { MappingPredicate } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderBuilder(
  value: MappingPredicate | null | undefined,
  onChange = vi.fn(),
  depth = 0,
) {
  return render(
    <WhenPredicateBuilder value={value} onChange={onChange} depth={depth} />,
  )
}

// ── Sem predicado ─────────────────────────────────────────────────────────────

describe("WhenPredicateBuilder — sem predicado", () => {
  it("exibe botão 'Adicionar condição' quando value é null", () => {
    renderBuilder(null)
    expect(screen.getByTestId("when-add-condition")).toBeInTheDocument()
  })

  it("exibe botão 'Adicionar condição' quando value é undefined", () => {
    renderBuilder(undefined)
    expect(screen.getByTestId("when-add-condition")).toBeInTheDocument()
  })

  it("clicar em 'Adicionar condição' chama onChange com { exists: '' }", () => {
    const onChange = vi.fn()
    renderBuilder(null, onChange)
    fireEvent.click(screen.getByTestId("when-add-condition"))
    expect(onChange).toHaveBeenCalledWith({ exists: "" })
  })
})

// ── Operador exists ───────────────────────────────────────────────────────────

describe("WhenPredicateBuilder — operador exists", () => {
  it("renderiza input de source quando predicado é { exists: '' }", () => {
    renderBuilder({ exists: "" })
    expect(screen.getByTestId("when-exists-source")).toBeInTheDocument()
  })

  it("exibe aviso de source vazio quando source é ''", () => {
    renderBuilder({ exists: "" })
    expect(screen.getByTestId("when-source-empty-warning")).toBeInTheDocument()
  })

  it("sem aviso quando source tem valor", () => {
    renderBuilder({ exists: "data.severity" })
    expect(screen.queryByTestId("when-source-empty-warning")).not.toBeInTheDocument()
  })

  it("alterar source chama onChange com novo predicado exists", () => {
    const onChange = vi.fn()
    renderBuilder({ exists: "" }, onChange)
    fireEvent.change(screen.getByTestId("when-exists-source"), {
      target: { value: "data.alert" },
    })
    expect(onChange).toHaveBeenCalledWith({ exists: "data.alert" })
  })

  it("clicar em Remover chama onChange(null)", () => {
    const onChange = vi.fn()
    renderBuilder({ exists: "data.alert" }, onChange)
    fireEvent.click(screen.getByTestId("when-remove-condition"))
    expect(onChange).toHaveBeenCalledWith(null)
  })
})

// ── Operador equals ───────────────────────────────────────────────────────────

describe("WhenPredicateBuilder — operador equals", () => {
  const pred: MappingPredicate = { equals: { source: "data.severity", value: "high" } }

  it("renderiza inputs de source e value", () => {
    renderBuilder(pred)
    expect(screen.getByTestId("when-equals-source")).toBeInTheDocument()
    expect(screen.getByTestId("when-equals-value")).toBeInTheDocument()
  })

  it("alterar source emite predicado correto", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-equals-source"), {
      target: { value: "data.level" },
    })
    expect(onChange).toHaveBeenCalledWith({
      equals: { source: "data.level", value: "high" },
    })
  })

  it("alterar value para string emite como string", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-equals-value"), {
      target: { value: "critical" },
    })
    expect(onChange).toHaveBeenCalledWith({
      equals: { source: "data.severity", value: "critical" },
    })
  })

  it("alterar value para número emite como number", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-equals-value"), {
      target: { value: "42" },
    })
    expect(onChange).toHaveBeenCalledWith({
      equals: { source: "data.severity", value: 42 },
    })
  })

  it("exibe aviso de source vazio", () => {
    renderBuilder({ equals: { source: "", value: "x" } })
    expect(screen.getByTestId("when-source-empty-warning")).toBeInTheDocument()
  })
})

// ── Operador in ───────────────────────────────────────────────────────────────

describe("WhenPredicateBuilder — operador in", () => {
  const pred: MappingPredicate = {
    in: { source: "data.severity", values: ["high", "critical"] },
  }

  it("renderiza inputs de source e values", () => {
    renderBuilder(pred)
    expect(screen.getByTestId("when-in-source")).toBeInTheDocument()
    expect(screen.getByTestId("when-in-values")).toBeInTheDocument()
  })

  it("alterar source emite predicado correto", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-in-source"), {
      target: { value: "data.level" },
    })
    expect(onChange).toHaveBeenCalledWith({
      in: { source: "data.level", values: ["high", "critical"] },
    })
  })

  it("alterar values (strings) emite lista correta", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-in-values"), {
      target: { value: "low\nmedium" },
    })
    expect(onChange).toHaveBeenCalledWith({
      in: { source: "data.severity", values: ["low", "medium"] },
    })
  })

  it("valores numéricos no textarea são parseados como number", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-in-values"), {
      target: { value: "1\n2\n3" },
    })
    expect(onChange).toHaveBeenCalledWith({
      in: { source: "data.severity", values: [1, 2, 3] },
    })
  })

  it("linhas vazias são ignoradas no parse", () => {
    const onChange = vi.fn()
    renderBuilder(pred, onChange)
    fireEvent.change(screen.getByTestId("when-in-values"), {
      target: { value: "high\n\ncritical\n" },
    })
    expect(onChange).toHaveBeenCalledWith({
      in: { source: "data.severity", values: ["high", "critical"] },
    })
  })
})

// ── Operador not ──────────────────────────────────────────────────────────────

describe("WhenPredicateBuilder — operador not", () => {
  it("renderiza sub-builder para o filho do not", () => {
    renderBuilder({ not: { exists: "data.field" } })
    // Dois builders: o pai (not) e o filho (exists)
    const builders = screen.getAllByTestId("when-predicate-builder")
    expect(builders.length).toBe(2)
  })

  it("alterar filho emite predicado not com filho atualizado", () => {
    const onChange = vi.fn()
    renderBuilder({ not: { exists: "" } }, onChange)
    // O filho exibe input de source
    fireEvent.change(screen.getByTestId("when-exists-source"), {
      target: { value: "x.y.z" },
    })
    expect(onChange).toHaveBeenCalledWith({ not: { exists: "x.y.z" } })
  })

  it("remover filho do not volta para placeholder { exists: '' }", () => {
    const onChange = vi.fn()
    renderBuilder({ not: { exists: "data.field" } }, onChange)
    // O botão Remover do filho tem aria-label "Remover condição when"
    const removeBtns = screen.getAllByTestId("when-remove-condition")
    // O segundo remover é do filho
    fireEvent.click(removeBtns[1])
    expect(onChange).toHaveBeenCalledWith({ not: { exists: "" } })
  })
})

// ── Troca de operador ─────────────────────────────────────────────────────────

describe("WhenPredicateBuilder — troca de operador via Select", () => {
  it("trocar de exists para equals emite predicado equals com source vazio", () => {
    const onChange = vi.fn()
    renderBuilder({ exists: "data.field" }, onChange)

    // Identifica o trigger do Select pelo aria-haspopup="listbox"
    const opTrigger = screen
      .getAllByRole("button")
      .find((btn) => btn.getAttribute("aria-haspopup") === "listbox")!
    fireEvent.click(opTrigger)
    fireEvent.click(screen.getByRole("option", { name: /equals/i }))
    expect(onChange).toHaveBeenCalledWith({ equals: { source: "", value: "" } })
  })

  it("trocar para not emite { not: { exists: '' } }", () => {
    const onChange = vi.fn()
    renderBuilder({ exists: "" }, onChange)
    const opTrigger = screen
      .getAllByRole("button")
      .find((btn) => btn.getAttribute("aria-haspopup") === "listbox")!
    fireEvent.click(opTrigger)
    fireEvent.click(screen.getByRole("option", { name: /not/i }))
    expect(onChange).toHaveBeenCalledWith({ not: { exists: "" } })
  })

  it("trocar para in emite { in: { source: '', values: [] } }", () => {
    const onChange = vi.fn()
    renderBuilder({ exists: "" }, onChange)
    const opTrigger = screen
      .getAllByRole("button")
      .find((btn) => btn.getAttribute("aria-haspopup") === "listbox")!
    fireEvent.click(opTrigger)
    fireEvent.click(screen.getByRole("option", { name: /^in/i }))
    expect(onChange).toHaveBeenCalledWith({ in: { source: "", values: [] } })
  })
})

// ── Limite de profundidade ────────────────────────────────────────────────────

describe("WhenPredicateBuilder — limite de profundidade", () => {
  it("exibe mensagem de erro em depth=4", () => {
    renderBuilder({ exists: "x" }, vi.fn(), 4)
    expect(screen.getByTestId("when-max-depth")).toBeInTheDocument()
    expect(screen.queryByTestId("when-predicate-builder")).not.toBeInTheDocument()
  })

  it("profundidade 3 ainda renderiza normalmente", () => {
    renderBuilder({ exists: "x" }, vi.fn(), 3)
    expect(screen.getByTestId("when-predicate-builder")).toBeInTheDocument()
    expect(screen.queryByTestId("when-max-depth")).not.toBeInTheDocument()
  })
})
