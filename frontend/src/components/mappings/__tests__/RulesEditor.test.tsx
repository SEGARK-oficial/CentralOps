/**
 * Testes de RulesEditor
 * Sprint 1: render de N regras, badges corretos, estado vazio, expansão de detalhes.
 * Sprint 3: busca, filtros chip, collapse-all, contador, agrupamento.
 */

import React from "react"
import { render, screen, fireEvent } from "@testing-library/react"
import { RulesEditor } from "@/components/mappings/RulesEditor"
import { OCSF_TEMPLATES } from "@/data/ocsfTemplates"
import type { MappingRule } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const RULES: MappingRule[] = [
  { target: "event.action", source: "action" },
  {
    target: "event.severity",
    source: "severity",
    required: true,
    type_cast: "to_str",
    value_map: { "1": "low", "2": "medium", "3": "high" },
  },
  {
    target: "event.const_field",
    const: "fixed_value",
    default: "fallback",
  },
]

// ── Regressão Sprint 1 ─────────────────────────────────────────────────────────

describe("RulesEditor — regressão Sprint 1", () => {
  it("renderiza todas as regras passadas via props", () => {
    render(<RulesEditor rules={RULES} />)

    expect(screen.getByTestId("rule-row-event.action")).toBeInTheDocument()
    expect(screen.getByTestId("rule-row-event.severity")).toBeInTheDocument()
    expect(screen.getByTestId("rule-row-event.const_field")).toBeInTheDocument()
  })

  it("exibe badge com contagem total quando sem filtro (plural)", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.getByText("Total: 3 regras")).toBeInTheDocument()
  })

  it("exibe badge singular para 1 regra", () => {
    render(<RulesEditor rules={[RULES[0]]} />)
    expect(screen.getByText("Total: 1 regra")).toBeInTheDocument()
  })

  it("exibe EmptyState quando rules=[]", () => {
    render(<RulesEditor rules={[]} />)
    expect(screen.getByText("Nenhuma regra definida")).toBeInTheDocument()
  })

  it("exibe badge 'obrigatório' na regra com required=true", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.getByText("obrigatório")).toBeInTheDocument()
  })

  it("exibe badge de type_cast na regra com type_cast", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.getByText("to_str")).toBeInTheDocument()
  })

  it("exibe badge 'value_map' na regra com value_map", () => {
    render(<RulesEditor rules={RULES} />)
    // Pode haver múltiplos "value_map" (badge na regra + chip de filtro)
    const matches = screen.getAllByText("value_map")
    expect(matches.length).toBeGreaterThanOrEqual(1)
  })

  it("painel tem role=region com aria-labelledby", () => {
    render(<RulesEditor rules={RULES} />)
    const region = screen.getByTestId("rules-editor")
    expect(region).toHaveAttribute("role", "region")
    expect(region).toHaveAttribute("aria-labelledby")
  })

  it("expansão inline mostra detalhes ao clicar no chevron (uncontrolled em view)", () => {
    render(<RulesEditor rules={RULES} />)

    const expandBtn = screen.getByTestId("rule-row-event.severity").querySelector(
      "button[aria-expanded]",
    )
    expect(expandBtn).not.toBeNull()

    fireEvent.click(expandBtn!)

    expect(screen.getByText(/value_map:/)).toBeInTheDocument()
  })

  it("expansão inline mostra default quando regra tem default", () => {
    render(<RulesEditor rules={RULES} />)

    const expandBtn = screen.getByTestId("rule-row-event.const_field").querySelector(
      "button[aria-expanded]",
    )
    expect(expandBtn).not.toBeNull()

    fireEvent.click(expandBtn!)

    expect(screen.getByText(/default:/)).toBeInTheDocument()
  })

  it("regra sem detalhes não exibe botão de expansão", () => {
    render(<RulesEditor rules={[RULES[0]]} />)

    const expandBtn = screen.getByTestId("rule-row-event.action").querySelector(
      "button[aria-expanded]",
    )
    expect(expandBtn).toBeNull()
  })
})

// ── Sprint 3: Busca ────────────────────────────────────────────────────────────

describe("RulesEditor — busca", () => {
  it("busca por target filtra as regras exibidas", () => {
    render(<RulesEditor rules={RULES} />)

    const searchInput = screen.getByTestId("rules-search")
    fireEvent.change(searchInput, { target: { value: "event.action" } })

    expect(screen.getByTestId("rule-row-event.action")).toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.severity")).not.toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.const_field")).not.toBeInTheDocument()
  })

  it("busca por source filtra as regras exibidas", () => {
    render(<RulesEditor rules={RULES} />)

    const searchInput = screen.getByTestId("rules-search")
    fireEvent.change(searchInput, { target: { value: "severity" } })

    expect(screen.getByTestId("rule-row-event.severity")).toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.action")).not.toBeInTheDocument()
  })

  it("busca case-insensitive", () => {
    render(<RulesEditor rules={RULES} />)

    const searchInput = screen.getByTestId("rules-search")
    fireEvent.change(searchInput, { target: { value: "EVENT.ACTION" } })

    expect(screen.getByTestId("rule-row-event.action")).toBeInTheDocument()
  })

  it("busca sem resultado exibe mensagem de nenhuma regra encontrada", () => {
    render(<RulesEditor rules={RULES} />)

    const searchInput = screen.getByTestId("rules-search")
    fireEvent.change(searchInput, { target: { value: "zzz_inexistente" } })

    expect(screen.getByTestId("no-results")).toBeInTheDocument()
  })
})

// ── Sprint 3: Filtros ──────────────────────────────────────────────────────────

describe("RulesEditor — filtros chip", () => {
  it("chip 'Obrigatórias' filtra e exibe apenas regras required=true", () => {
    render(<RulesEditor rules={RULES} />)

    fireEvent.click(screen.getByTestId("filter-required"))

    // Só event.severity tem required=true
    expect(screen.getByTestId("rule-row-event.severity")).toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.action")).not.toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.const_field")).not.toBeInTheDocument()
  })

  it("chip 'value_map' filtra e exibe apenas regras com value_map", () => {
    render(<RulesEditor rules={RULES} />)

    fireEvent.click(screen.getByTestId("filter-value-map"))

    expect(screen.getByTestId("rule-row-event.severity")).toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.action")).not.toBeInTheDocument()
  })

  it("chip 'type_cast' filtra e exibe apenas regras com type_cast", () => {
    render(<RulesEditor rules={RULES} />)

    fireEvent.click(screen.getByTestId("filter-type-cast"))

    expect(screen.getByTestId("rule-row-event.severity")).toBeInTheDocument()
    expect(screen.queryByTestId("rule-row-event.action")).not.toBeInTheDocument()
  })

  it("chip ativo tem aria-pressed=true", () => {
    render(<RulesEditor rules={RULES} />)

    const chip = screen.getByTestId("filter-required")
    expect(chip).toHaveAttribute("aria-pressed", "false")

    fireEvent.click(chip)
    expect(chip).toHaveAttribute("aria-pressed", "true")
  })
})

// ── Sprint 3: Contador ─────────────────────────────────────────────────────────

describe("RulesEditor — contador", () => {
  it("sem filtro NÃO exibe contador de filtro", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.queryByTestId("rules-counter")).not.toBeInTheDocument()
  })

  it("com filtro exibe contador de regras visíveis", () => {
    render(<RulesEditor rules={RULES} />)

    const searchInput = screen.getByTestId("rules-search")
    fireEvent.change(searchInput, { target: { value: "event.action" } })

    const counter = screen.getByTestId("rules-counter")
    expect(counter).toHaveTextContent("Mostrando 1 de 3 regras")
  })

  it("contador atualiza ao mudar o filtro", () => {
    render(<RulesEditor rules={RULES} />)

    fireEvent.click(screen.getByTestId("filter-required"))

    const counter = screen.getByTestId("rules-counter")
    expect(counter).toHaveTextContent("Mostrando 1 de 3 regras")
  })
})

// ── Sprint 3: Collapse all ─────────────────────────────────────────────────────

describe("RulesEditor — collapse all", () => {
  it("botão 'Recolher tudo' existe", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.getByTestId("collapse-all")).toBeInTheDocument()
  })

  it("após expandir uma regra, 'Recolher tudo' fecha ela (aria-expanded=false)", () => {
    render(<RulesEditor rules={RULES} />)

    // Expande event.severity
    const expandBtn = screen.getByTestId("rule-row-event.severity").querySelector(
      "button[aria-expanded]",
    )!
    fireEvent.click(expandBtn)
    expect(expandBtn).toHaveAttribute("aria-expanded", "true")

    // Colapsa tudo
    fireEvent.click(screen.getByTestId("collapse-all"))

    // Agora aria-expanded deve ser false
    const expandBtnAfter = screen.getByTestId("rule-row-event.severity").querySelector(
      "button[aria-expanded]",
    )!
    expect(expandBtnAfter).toHaveAttribute("aria-expanded", "false")
  })
})

describe("RulesEditor — edit mode collapse", () => {
  it("regras vêm colapsadas por padrão em edit mode", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)
    // Em edit mode colapsado, body de cada row não é renderizado: nenhum
    // tooltip "Ajuda: target" aparece.
    expect(screen.queryByRole("button", { name: /ajuda: target/i })).toBeNull()
  })

  it("'Expandir tudo' aparece apenas em edit mode e abre todas as regras", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)
    fireEvent.click(screen.getByTestId("expand-all"))
    // Após expandir tudo, todas as regras mostram tooltip de "Ajuda: target"
    const targetTooltips = screen.getAllByRole("button", { name: /ajuda: target/i })
    expect(targetTooltips.length).toBe(RULES.length)
  })

  it("'Expandir tudo' NÃO aparece em view mode", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.queryByTestId("expand-all")).toBeNull()
  })

  it("clicar 'Adicionar regra' abre o dropdown (não adiciona imediatamente)", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))

    // O menu deve aparecer
    expect(screen.getByTestId("add-rule-menu")).toBeInTheDocument()
    // onChange não foi chamado ainda
    expect(onChange).not.toHaveBeenCalled()
  })

  it("selecionar 'Regra escalar' no dropdown cria regra scalar", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("add-scalar-rule"))

    expect(onChange).toHaveBeenCalledTimes(1)
    const updatedRules = onChange.mock.calls[0][0]
    expect(updatedRules.length).toBe(RULES.length + 1)
    const newRule = updatedRules[updatedRules.length - 1]
    expect(newRule.target).toMatch(/^novo\.campo/)
    expect(newRule.kind).toBeUndefined() // scalar não serializa kind
  })
})

// ── Fase 3.3: Add rule dropdown ───────────────────────────────────────────────

describe("RulesEditor — dropdown de adicionar regra", () => {
  it("dropdown existe em edit mode e exibe ambas as opções", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))

    expect(screen.getByTestId("add-scalar-rule")).toBeInTheDocument()
    expect(screen.getByTestId("add-array-builder-rule")).toBeInTheDocument()
  })

  it("botão de adicionar NÃO aparece em view mode", () => {
    render(<RulesEditor rules={RULES} />)
    expect(screen.queryByTestId("add-rule-button")).not.toBeInTheDocument()
  })

  it("'Array builder (observables)' cria regra com kind='array_builder'", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("add-array-builder-rule"))

    expect(onChange).toHaveBeenCalledTimes(1)
    const updatedRules = onChange.mock.calls[0][0]
    const newRule = updatedRules[updatedRules.length - 1]
    expect(newRule.kind).toBe("array_builder")
    expect(newRule.target).toBe("normalized.observables")
    expect(newRule.items).toEqual([])
    expect(newRule.skip_null).toBe(true)
  })

  it("depois de selecionar uma opção, o menu fecha", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))
    expect(screen.getByTestId("add-rule-menu")).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("add-scalar-rule"))
    expect(screen.queryByTestId("add-rule-menu")).not.toBeInTheDocument()
  })

  it("dropdown tem aria-expanded=true quando aberto", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    const btn = screen.getByTestId("add-rule-button")
    expect(btn).toHaveAttribute("aria-expanded", "false")

    fireEvent.click(btn)
    expect(btn).toHaveAttribute("aria-expanded", "true")
  })

  it("mapping com apenas regras escalares renderiza identicamente em view mode (sem regressão)", () => {
    const { container } = render(<RulesEditor rules={RULES} />)
    // Confirma que os 3 rule-rows escalares continuam renderizando
    expect(container.querySelectorAll("[data-testid^='rule-row-']")).toHaveLength(RULES.length)
    // Nenhum chip array_builder deve aparecer
    expect(container.querySelectorAll("[data-testid='array-builder-chip']")).toHaveLength(0)
  })
})

// ── Bug 1: foco estável no input target durante edição ────────────────────────

describe("RulesEditor — foco estável no input target", () => {
  it("digitar no input target não remonta a row (DOM node permanece o mesmo)", () => {
    // Para que o componente reflita a edição no input (display value), precisamos
    // de um componente controlado real — usamos um wrapper com estado local.
    const Wrapper: React.FC = () => {
      const [rules, setRules] = React.useState<MappingRule[]>([
        { target: "event.action", source: "action" },
      ])
      return <RulesEditor rules={rules} mode="edit" onChange={setRules} />
    }

    render(<Wrapper />)

    // Expande a única row para acessar os inputs
    const expandBtn = screen.getAllByRole("button", { name: /expandir regra/i })[0]
    fireEvent.click(expandBtn)

    // Obtém o input de target antes da edição e registra o DOM node
    const inputBefore = screen.getByDisplayValue("event.action")
    inputBefore.focus()
    expect(document.activeElement).toBe(inputBefore)

    // Simula digitação de um caractere
    fireEvent.change(inputBefore, { target: { value: "event.action_x" } })

    // O input deve refletir o novo valor (estado propagado pelo wrapper)
    const inputAfter = screen.getByDisplayValue("event.action_x")

    // Mesmo DOM node — sem remount. Com a key antiga (target+index), o node
    // seria desmontado e inputAfter seria uma referência diferente de inputBefore.
    expect(inputAfter).toBe(inputBefore)
  })
})

// ── Fase 4.2: Template OCSF ───────────────────────────────────────────────────

describe("RulesEditor — template OCSF", () => {
  it("dropdown exibe a opção 'Carregar template OCSF…' em edit mode", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))

    expect(screen.getByTestId("load-ocsf-template")).toBeInTheDocument()
    expect(screen.getByText("Carregar template OCSF…")).toBeInTheDocument()
  })

  it("'Carregar template OCSF…' NÃO aparece em view mode", () => {
    render(<RulesEditor rules={RULES} />)
    // Em view mode o dropdown inteiro não existe
    expect(screen.queryByTestId("load-ocsf-template")).not.toBeInTheDocument()
  })

  it("clicar em 'Carregar template OCSF…' abre o TemplatePicker", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("load-ocsf-template"))

    // O modal do TemplatePicker deve aparecer
    expect(screen.getByTestId("template-picker")).toBeInTheDocument()
  })

  it("clicar em 'Carregar template OCSF…' fecha o dropdown", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={RULES} mode="edit" onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule-button"))
    expect(screen.getByTestId("add-rule-menu")).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("load-ocsf-template"))

    expect(screen.queryByTestId("add-rule-menu")).not.toBeInTheDocument()
  })

  it("escolher template com editor vazio chama onChange com as regras do template", () => {
    const onChange = vi.fn()
    render(<RulesEditor rules={[]} mode="edit" onChange={onChange} />)

    // Abre o picker via dropdown
    fireEvent.click(screen.getByTestId("add-rule-button"))
    fireEvent.click(screen.getByTestId("load-ocsf-template"))

    // Clica em "Usar template" no primeiro template
    const firstTemplate = OCSF_TEMPLATES[0]
    fireEvent.click(screen.getByTestId(`use-template-${firstTemplate.id}`))

    expect(onChange).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledWith(firstTemplate.rules)
  })
})
