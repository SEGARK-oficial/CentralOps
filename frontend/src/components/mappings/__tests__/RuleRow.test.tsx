/**
 * Testes de RuleRow
 * Cobre: modo view, modo edit com tooltips, controlled expand.
 * Fase 1.3: dropdown de type_cast dinâmico (useTypeCasts).
 */

import type React from "react"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { RuleRow } from "@/components/mappings/RuleRow"
import type { MappingRule } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// ── Mock de useTypeCasts ──────────────────────────────────────────────────────
// Os testes existentes não precisam de dados reais; o mock retorna estado
// resolvido por padrão para não quebrar o comportamento anterior.

vi.mock("@/hooks/useTypeCasts", () => ({
  useTypeCasts: vi.fn(() => ({
    data: [
      { name: "epoch_to_iso", description: "Epoch ms para ISO 8601", signature: "epoch_to_iso(ts: int) -> str" },
      { name: "iso_to_epoch", description: "ISO 8601 para epoch ms", signature: "iso_to_epoch(ts: str) -> int" },
      { name: "to_bool", description: "Coerce para bool", signature: "to_bool(value: any) -> bool" },
      { name: "to_int", description: "Coerce para inteiro", signature: "to_int(value: any) -> int" },
      { name: "to_str", description: "Coerce para string", signature: "to_str(value: any) -> str" },
    ],
    loading: false,
    error: null,
  })),
}))

import { useTypeCasts } from "@/hooks/useTypeCasts"
const mockedUseTypeCasts = vi.mocked(useTypeCasts)

const RULE_SIMPLE: MappingRule = {
  target: "event.action",
  source: "action",
}

const RULE_FULL: MappingRule = {
  target: "event.severity",
  source: "severity",
  required: true,
  type_cast: "to_str",
  value_map: { high: 4, medium: 3, low: 1 },
  default: "0",
}

describe("RuleRow — view mode", () => {
  it("renderiza target e source em modo view", () => {
    render(<RuleRow rule={RULE_SIMPLE} mode="view" />)
    expect(screen.getByText("event.action")).toBeInTheDocument()
    expect(screen.getByText("action")).toBeInTheDocument()
  })

  it("exibe badges de required, type_cast e value_map quando presentes", () => {
    render(<RuleRow rule={RULE_FULL} mode="view" />)
    expect(screen.getByText("obrigatório")).toBeInTheDocument()
    expect(screen.getByText("to_str")).toBeInTheDocument()
    expect(screen.getByText("value_map")).toBeInTheDocument()
  })

  it("sem details não exibe botão de expansão", () => {
    render(<RuleRow rule={RULE_SIMPLE} mode="view" />)
    expect(screen.queryByRole("button", { name: /expandir/i })).not.toBeInTheDocument()
  })

  it("com details exibe botão de expansão", () => {
    render(<RuleRow rule={RULE_FULL} mode="view" />)
    expect(screen.getByRole("button", { name: /expandir|recolher/i })).toBeInTheDocument()
  })

  it("modo uncontrolled: expande ao clicar no chevron", () => {
    render(<RuleRow rule={RULE_FULL} mode="view" />)
    const btn = screen.getByRole("button", { name: /expandir/i })
    fireEvent.click(btn)
    expect(screen.getByText(/value_map:/)).toBeInTheDocument()
  })

  it("modo controlled: usa expanded prop e chama onToggleExpand", () => {
    const onToggle = vi.fn()
    render(
      <RuleRow
        rule={RULE_FULL}
        mode="view"
        expanded={false}
        onToggleExpand={onToggle}
      />,
    )
    expect(screen.queryByText(/value_map:/)).not.toBeInTheDocument()

    const btn = screen.getByRole("button", { name: /expandir/i })
    fireEvent.click(btn)
    expect(onToggle).toHaveBeenCalledTimes(1)
  })

  it("modo controlled expanded=true mostra detalhes", () => {
    render(
      <RuleRow
        rule={RULE_FULL}
        mode="view"
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByText(/value_map:/)).toBeInTheDocument()
  })
})

// Helper: cria props de edit mode com index e callbacks estáveis
function editProps(overrides: Partial<React.ComponentProps<typeof RuleRow>> = {}) {
  return {
    index: 0,
    onChange: vi.fn(),
    onRemove: vi.fn(),
    onMoveUp: vi.fn(),
    onMoveDown: vi.fn(),
    ...overrides,
  }
}

describe("RuleRow — edit mode tooltips", () => {
  it("renderiza o tooltip de ajuda do campo target", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    // O trigger do HelpTooltip para "target" deve estar visível
    const helpBtn = screen.getByRole("button", { name: /ajuda: target/i })
    expect(helpBtn).toBeInTheDocument()
  })

  it("tooltip do campo target abre e exibe example ao clicar", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    const helpBtn = screen.getByRole("button", { name: /ajuda: target/i })
    fireEvent.click(helpBtn)

    // Tooltip deve estar visível com o example
    expect(screen.getByRole("tooltip")).toBeInTheDocument()
    const codeEl = screen.getByText("normalized.severity_id")
    expect(codeEl.tagName).toBe("CODE")
  })

  it("tooltip do campo value_map exibe example de JSON em <code>", () => {
    render(
      <RuleRow
        rule={RULE_FULL}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    const helpBtn = screen.getByRole("button", { name: /ajuda: value_map/i })
    fireEvent.click(helpBtn)

    expect(screen.getByRole("tooltip")).toBeInTheDocument()

    // O example do value_map é renderizado num <code> dentro do tooltip
    const tooltip = screen.getByRole("tooltip")
    const codeEl = tooltip.querySelector("code")
    expect(codeEl).not.toBeNull()
    expect(codeEl!.textContent).toMatch(/high/)
  })

  it("todos os campos de edit mode têm seu HelpTooltip trigger", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    // Espera triggers para: target, Tipo de fonte, source (JMESPath), default, value_map, type_cast, Obrigatório, valor padrão intencional
    expect(screen.getByRole("button", { name: /ajuda: target/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /ajuda: tipo de fonte/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /ajuda: source \(jmespath\)/i })).toBeInTheDocument()
    // Usa expressão exata para não confundir com "Ajuda: valor padrão intencional"
    expect(screen.getByRole("button", { name: /^ajuda: default$/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /ajuda: value_map/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /ajuda: type_cast/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /ajuda: obrigatório/i })).toBeInTheDocument()
  })
})

describe("RuleRow — edit mode collapse", () => {
  it("vem colapsado por padrão (uncontrolled)", () => {
    render(
      <RuleRow
        rule={RULE_FULL}
        mode="edit"
        {...editProps()}
      />,
    )
    // Sem prop expanded, body fica oculto: tooltip do target não aparece.
    expect(screen.queryByRole("button", { name: /ajuda: target/i })).toBeNull()
    // Header compacto sempre visível: target preview + botão Remover.
    expect(screen.getByText("event.severity")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /^remover$/i })).toBeInTheDocument()
  })

  it("expande ao clicar no chevron (uncontrolled)", () => {
    render(
      <RuleRow
        rule={RULE_FULL}
        mode="edit"
        {...editProps()}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /expandir regra/i }))
    expect(screen.getByRole("button", { name: /ajuda: target/i })).toBeInTheDocument()
  })

  it("respeita prop expanded controlled", () => {
    const onToggle = vi.fn()
    const stableProps = editProps()
    const { rerender } = render(
      <RuleRow
        rule={RULE_FULL}
        mode="edit"
        {...stableProps}
        expanded={false}
        onToggleExpand={onToggle}
      />,
    )
    // Body oculto
    expect(screen.queryByRole("button", { name: /ajuda: target/i })).toBeNull()

    // Click no chevron deve chamar onToggle, não atualizar estado interno
    fireEvent.click(screen.getByRole("button", { name: /expandir regra/i }))
    expect(onToggle).toHaveBeenCalledTimes(1)
    // Continua oculto até o pai mudar a prop
    expect(screen.queryByRole("button", { name: /ajuda: target/i })).toBeNull()

    rerender(
      <RuleRow
        rule={RULE_FULL}
        mode="edit"
        {...stableProps}
        expanded={true}
        onToggleExpand={onToggle}
      />,
    )
    expect(screen.getByRole("button", { name: /ajuda: target/i })).toBeInTheDocument()
  })
})

describe("HelpTooltip — interação click + hover", () => {
  it("click após hover MANTÉM tooltip aberto (não fecha)", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    const helpBtn = screen.getByRole("button", { name: /ajuda: target/i })
    // Simula hover: tooltip abre via mouseEnter
    fireEvent.mouseEnter(helpBtn)
    expect(screen.getByRole("tooltip")).toBeInTheDocument()

    // Agora o usuário clica no ícone — antes da fix isso fechava.
    // Comportamento esperado: clicar fixa o tooltip aberto.
    fireEvent.click(helpBtn)
    expect(screen.getByRole("tooltip")).toBeInTheDocument()

    // Mouse sai — tooltip permanece (sticky via click).
    fireEvent.mouseLeave(helpBtn)
    expect(screen.queryByRole("tooltip")).toBeInTheDocument()
  })
})

// ── Fase 1.3: dropdown type_cast dinâmico ────────────────────────────────────

describe("RuleRow — type_cast dropdown dinâmico", () => {
  afterEach(() => {
    // Restaura mock padrão após cada teste que o modifica.
    mockedUseTypeCasts.mockReturnValue({
      data: [
        { name: "epoch_to_iso", description: "Epoch ms para ISO 8601", signature: "epoch_to_iso(ts: int) -> str" },
        { name: "iso_to_epoch", description: "ISO 8601 para epoch ms", signature: "iso_to_epoch(ts: str) -> int" },
        { name: "to_bool", description: "Coerce para bool", signature: "to_bool(value: any) -> bool" },
        { name: "to_int", description: "Coerce para inteiro", signature: "to_int(value: any) -> int" },
        { name: "to_str", description: "Coerce para string", signature: "to_str(value: any) -> str" },
      ],
      loading: false,
      error: null,
    })
  })

  it("renderiza opções dinamicamente após resolução do hook", async () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    // Abre o dropdown clicando no trigger do Select de type_cast.
    // Identifica o trigger pelo aria-haspopup="listbox" (único no componente).
    const typecastTrigger = screen.getAllByRole("button").find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    expect(typecastTrigger).toBeDefined()
    fireEvent.click(typecastTrigger)

    // Opções dinâmicas devem aparecer no listbox.
    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Nenhum" })).toBeInTheDocument()
      expect(screen.getByRole("option", { name: "iso_to_epoch" })).toBeInTheDocument()
      expect(screen.getByRole("option", { name: "epoch_to_iso" })).toBeInTheDocument()
      expect(screen.getByRole("option", { name: "to_str" })).toBeInTheDocument()
    })
  })

  it("regra existente com type_cast 'epoch_to_iso' continua sendo exibida no select e no badge", () => {
    const ruleWithCast: MappingRule = { target: "ts", source: "timestamp", type_cast: "epoch_to_iso" }
    render(
      <RuleRow
        rule={ruleWithCast}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    // O trigger do Select deve exibir o nome do cast atual como texto.
    const selectTrigger = screen.getAllByRole("button").find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    expect(selectTrigger).toHaveTextContent("epoch_to_iso")

    // Badge no header compacto (pode haver múltiplas ocorrências do texto — usa getAllByText).
    expect(screen.getAllByText("epoch_to_iso").length).toBeGreaterThanOrEqual(1)
  })

  it("estado loading: dropdown desabilitado, exibe texto 'Carregando...'", () => {
    mockedUseTypeCasts.mockReturnValue({ data: null, loading: true, error: null })

    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    // O Select trigger está desabilitado e o texto do placeholder é "Carregando..."
    const selectTrigger = screen.getAllByRole("button").find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    expect(selectTrigger).toBeDisabled()
    expect(selectTrigger).toHaveTextContent("Carregando...")
  })

  it("estado error: dropdown desabilitado, exibe texto 'Erro ao carregar'", () => {
    mockedUseTypeCasts.mockReturnValue({ data: null, loading: false, error: new Error("500") })

    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    const selectTrigger = screen.getAllByRole("button").find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    expect(selectTrigger).toBeDisabled()
    expect(selectTrigger).toHaveTextContent("Erro ao carregar")
  })

  it("selecionar uma opção dispara onChange com o nome do cast", async () => {
    const onChange = vi.fn()
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    // Abre o dropdown — identifica o trigger pelo aria-haspopup="listbox"
    const selectTrigger = screen.getAllByRole("button").find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    fireEvent.click(selectTrigger)

    // Clica em "to_int"
    await waitFor(() => screen.getByRole("option", { name: "to_int" }))
    fireEvent.click(screen.getByRole("option", { name: "to_int" }))

    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ type_cast: "to_int" }),
    )
  })

  it("selecionar 'Nenhum' emite type_cast=null", async () => {
    const onChange = vi.fn()
    const ruleWithCast: MappingRule = { target: "x", source: "x", type_cast: "to_str" }
    render(
      <RuleRow
        rule={ruleWithCast}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )

    const selectTrigger = screen.getAllByRole("button").find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    fireEvent.click(selectTrigger)

    await waitFor(() => screen.getByRole("option", { name: "Nenhum" }))
    fireEvent.click(screen.getByRole("option", { name: "Nenhum" }))

    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ type_cast: null }),
    )
  })

  it("editor não crasha quando error=true e rule tem type_cast definido", () => {
    mockedUseTypeCasts.mockReturnValue({ data: null, loading: false, error: new Error("fetch failed") })
    const ruleWithCast: MappingRule = { target: "ts", source: "ts", type_cast: "iso_to_epoch" }

    expect(() =>
      render(
        <RuleRow
          rule={ruleWithCast}
          mode="edit"
          {...editProps()}
          expanded={true}
          onToggleExpand={vi.fn()}
        />,
      ),
    ).not.toThrow()
  })
})

// ── Fase 3.3: array_builder dispatch ─────────────────────────────────────────

import type { ArrayBuilderRule } from "@/types"

const ARRAY_BUILDER_RULE: ArrayBuilderRule = {
  target: "normalized.observables",
  kind: "array_builder",
  items: [
    { name: "src_ip", type: "IP Address", type_id: 2, source: "data.clientIp" },
  ],
  skip_null: true,
}

describe("RuleRow — array_builder kind (view mode)", () => {
  it("renderiza o chip 'array_builder (1 item)' em view mode", () => {
    render(<RuleRow rule={ARRAY_BUILDER_RULE} mode="view" />)
    expect(screen.getByTestId("array-builder-chip")).toBeInTheDocument()
    expect(screen.getByTestId("array-builder-chip")).toHaveTextContent("array_builder (1 item)")
  })

  it("NOT renderiza o editor scalar em view mode para array_builder", () => {
    render(<RuleRow rule={ARRAY_BUILDER_RULE} mode="view" />)
    // Em view mode não há body expandível para array_builder — sem ArrayBuilderEditor
    expect(screen.queryByTestId("array-builder-editor")).not.toBeInTheDocument()
  })

  it("target aparece no header", () => {
    render(<RuleRow rule={ARRAY_BUILDER_RULE} mode="view" />)
    expect(screen.getByText("normalized.observables")).toBeInTheDocument()
  })
})

describe("RuleRow — array_builder kind (edit mode)", () => {
  it("em edit mode colapsado exibe chip array_builder no header", () => {
    render(
      <RuleRow
        rule={ARRAY_BUILDER_RULE}
        mode="edit"
        {...editProps()}
        expanded={false}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByTestId("array-builder-chip")).toBeInTheDocument()
  })

  it("em edit mode expandido renderiza ArrayBuilderEditor (não scalar editor)", () => {
    render(
      <RuleRow
        rule={ARRAY_BUILDER_RULE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByTestId("array-builder-editor")).toBeInTheDocument()
    // Não deve ter nenhum tooltip de campo scalar como "source (JMESPath)"
    expect(screen.queryByRole("button", { name: /ajuda: source \(jmespath\)/i })).not.toBeInTheDocument()
  })

  it("botão de remover está presente no header do array_builder", () => {
    render(
      <RuleRow
        rule={ARRAY_BUILDER_RULE}
        mode="edit"
        {...editProps()}
        expanded={false}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByRole("button", { name: /^remover$/i })).toBeInTheDocument()
  })

  it("array_builder com 0 items exibe '0 item' no chip", () => {
    const emptyRule: ArrayBuilderRule = {
      target: "normalized.observables",
      kind: "array_builder",
      items: [],
    }
    render(
      <RuleRow
        rule={emptyRule}
        mode="edit"
        {...editProps()}
        expanded={false}
        onToggleExpand={vi.fn()}
      />,
    )
    // CLDR pluraliza "pt" (não "pt-BR") com categoria "one" para n=0 (i18next _one).
    expect(screen.getByTestId("array-builder-chip")).toHaveTextContent("array_builder (0 item)")
  })
})

describe("RuleRow — scalar rule (backward compat)", () => {
  it("scalar rule sem kind renderiza editor scalar normalmente", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByRole("button", { name: /ajuda: target/i })).toBeInTheDocument()
    expect(screen.queryByTestId("array-builder-editor")).not.toBeInTheDocument()
    expect(screen.queryByTestId("array-builder-chip")).not.toBeInTheDocument()
  })

  it("scalar rule com kind='scalar' (explicito) também renderiza editor scalar", () => {
    const explicitScalar: MappingRule = { target: "x", source: "y", kind: "scalar" }
    render(
      <RuleRow
        rule={explicitScalar}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByRole("button", { name: /ajuda: target/i })).toBeInTheDocument()
    expect(screen.queryByTestId("array-builder-editor")).not.toBeInTheDocument()
  })
})

// ── Fase 4.1b: checkbox expected_always_default ───────────────────────────────

describe("RuleRow — expected_always_default checkbox (scalar, edit mode)", () => {
  it("renderiza o checkbox 'valor padrão intencional' em scalar edit mode expandido", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    const checkbox = screen.getByRole("checkbox", { name: /valor padrão intencional/i })
    expect(checkbox).toBeInTheDocument()
  })

  it("checkbox começa desmarcado quando expected_always_default é undefined", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    const checkbox = screen.getByRole("checkbox", { name: /valor padrão intencional/i })
    expect(checkbox).not.toBeChecked()
  })

  it("checkbox começa marcado quando expected_always_default=true na regra", () => {
    const ruleWithFlag: MappingRule = { target: "event.action", source: "action", expected_always_default: true }
    render(
      <RuleRow
        rule={ruleWithFlag}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    const checkbox = screen.getByRole("checkbox", { name: /valor padrão intencional/i })
    expect(checkbox).toBeChecked()
  })

  it("marcar o checkbox emite onChange com expected_always_default: true", () => {
    const onChange = vi.fn()
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    const checkbox = screen.getByRole("checkbox", { name: /valor padrão intencional/i })
    fireEvent.click(checkbox)
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ expected_always_default: true }),
    )
  })

  it("desmarcar o checkbox emite onChange com expected_always_default: false", () => {
    const onChange = vi.fn()
    const ruleWithFlag: MappingRule = { target: "event.action", source: "action", expected_always_default: true }
    render(
      <RuleRow
        rule={ruleWithFlag}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    const checkbox = screen.getByRole("checkbox", { name: /valor padrão intencional/i })
    fireEvent.click(checkbox)
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ expected_always_default: false }),
    )
  })

  it("checkbox tem associação explícita via htmlFor (label clicável)", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    // Queremos um label que wraps ou está associado ao checkbox
    const checkbox = screen.getByRole("checkbox", { name: /valor padrão intencional/i })
    const checkboxId = checkbox.getAttribute("id")
    expect(checkboxId).toBeTruthy()
    const label = document.querySelector(`label[for="${checkboxId}"]`)
    expect(label).not.toBeNull()
  })

  it("NÃO renderiza o checkbox 'valor padrão intencional' em array_builder mode", () => {
    render(
      <RuleRow
        rule={ARRAY_BUILDER_RULE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.queryByRole("checkbox", { name: /valor padrão intencional/i })).not.toBeInTheDocument()
  })
})

// ── Fase 2.3: campo when — WhenPredicateBuilder integrado ─────────────────────

describe("RuleRow — campo when (scalar, edit mode)", () => {
  it("renderiza a seção 'Condição (when)' em scalar edit mode expandido", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByRole("button", { name: /ajuda: condição \(when\)/i })).toBeInTheDocument()
  })

  it("exibe botão 'Adicionar condição' quando regra não tem when", () => {
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByTestId("when-add-condition")).toBeInTheDocument()
  })

  it("exibe builder preenchido quando regra tem when: { exists: 'x.y' }", () => {
    const ruleWithWhen: MappingRule = {
      target: "event.action",
      source: "action",
      when: { exists: "x.y" },
    }
    render(
      <RuleRow
        rule={ruleWithWhen}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.getByTestId("when-predicate-builder")).toBeInTheDocument()
    expect(screen.getByTestId("when-exists-source")).toHaveValue("x.y")
  })

  it("adicionar condição (when: null → exists) emite onChange com when.exists=''", () => {
    const onChange = vi.fn()
    render(
      <RuleRow
        rule={RULE_SIMPLE}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId("when-add-condition"))
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ when: { exists: "" } }),
    )
  })

  it("alterar operador para 'equals' emite onChange com when.equals", () => {
    const onChange = vi.fn()
    const ruleWithWhen: MappingRule = {
      target: "event.action",
      source: "action",
      when: { exists: "x.y" },
    }
    render(
      <RuleRow
        rule={ruleWithWhen}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    // Encontra o trigger do Select dentro do WhenPredicateBuilder via testid do container
    const whenBuilder = screen.getByTestId("when-predicate-builder")
    const opTrigger = Array.from(whenBuilder.querySelectorAll("button")).find(
      (btn) => btn.getAttribute("aria-haspopup") === "listbox",
    )!
    fireEvent.click(opTrigger)
    fireEvent.click(screen.getByRole("option", { name: /equals/i }))
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ when: { equals: { source: "", value: "" } } }),
    )
  })

  it("remover condição emite onChange com chave when AUSENTE (não null)", () => {
    const onChange = vi.fn()
    const ruleWithWhen: MappingRule = {
      target: "event.action",
      source: "action",
      when: { exists: "x.y" },
    }
    render(
      <RuleRow
        rule={ruleWithWhen}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByTestId("when-remove-condition"))
    const [, updatedRule] = onChange.mock.calls[0]
    // when deve estar AUSENTE, não null
    expect(updatedRule).not.toHaveProperty("when")
  })

  it("salvar com when: { exists: 'data.action' } inclui campo when no payload", () => {
    const onChange = vi.fn()
    const ruleWithWhen: MappingRule = {
      target: "event.action",
      source: "action",
      when: { exists: "data.action" },
    }
    render(
      <RuleRow
        rule={ruleWithWhen}
        mode="edit"
        {...editProps({ onChange })}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    // Altera source para triggerar onChange com a estrutura completa
    fireEvent.change(screen.getByTestId("when-exists-source"), {
      target: { value: "data.action.updated" },
    })
    expect(onChange).toHaveBeenCalledWith(
      0,
      expect.objectContaining({ when: { exists: "data.action.updated" } }),
    )
  })

  it("NÃO renderiza seção 'when' em array_builder mode", () => {
    render(
      <RuleRow
        rule={ARRAY_BUILDER_RULE}
        mode="edit"
        {...editProps()}
        expanded={true}
        onToggleExpand={vi.fn()}
      />,
    )
    expect(screen.queryByRole("button", { name: /ajuda: condição \(when\)/i })).not.toBeInTheDocument()
  })
})
