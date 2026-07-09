/**
 * Testes de DriftTable — Fase 4.3 + responsividade
 * Cobre: coluna "Mapeado por" (contagem singular/plural, "—", "…"),
 *        clique abre drawer-open callback, ações existentes preservadas,
 *        classes de responsividade nas colunas ocultas.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { DriftTable } from "@/components/normalization/DriftTable"
import * as permHooks from "@/hooks/usePermission"
import type { DriftEntry, PaginationConfig } from "@/types"
import type { FieldRulesIndex, MatchedRule } from "@/hooks/useFieldRules"

vi.mock("@/hooks/usePermission")
const mockedUsePermission = vi.mocked(permHooks.usePermission)

// ── Fixtures ──────────────────────────────────────────────────────────────────

const PAGINATION: PaginationConfig = {
  current: 1,
  pageSize: 20,
  showTotal: true,
  showSizeChanger: true,
}

const ENTRY_NEW: DriftEntry = {
  id: "d1",
  vendor: "sophos",
  event_type: "detection",
  field_path: "threat.actor",
  sample_value: "admin",
  sample_type: "string",
  occurrence_count: 42,
  first_seen: "2026-01-01T00:00:00Z",
  last_seen: "2026-01-02T00:00:00Z",
  status: "new",
}

const ENTRY_ZERO: DriftEntry = {
  id: "d2",
  vendor: "sophos",
  event_type: "detection",
  field_path: "unknown.path",
  sample_value: null,
  sample_type: null,
  occurrence_count: 1,
  first_seen: "2026-01-01T00:00:00Z",
  last_seen: "2026-01-02T00:00:00Z",
  status: "new",
}

const MATCHED_RULE: MatchedRule = {
  rule_target: "normalized.user",
  source: "threat.actor",
  match_kind: "primary",
  mapping_definition_id: "def-sophos",
  vendor: "sophos",
  event_type: "detection",
}

// ── FieldRulesIndex mocks ──────────────────────────────────────────────────────

function makeIndex(rules: MatchedRule[]): FieldRulesIndex {
  return {
    lookup: (_vendor: string, _et: string, path: string) => {
      return rules.filter((r) => r.source === path || path.startsWith(r.source + ".") || r.source.startsWith(path + "."))
    },
    count: function(v, et, path) { return this.lookup(v, et, path).length },
  }
}

const INDEX_ONE_RULE = makeIndex([MATCHED_RULE])
const INDEX_ZERO_RULES = makeIndex([])

const THREE_RULES: MatchedRule[] = [
  MATCHED_RULE,
  { ...MATCHED_RULE, rule_target: "normalized.actor", source: "threat.actor", match_kind: "fallback" },
  { ...MATCHED_RULE, rule_target: "normalized.id", source: "threat.actor", match_kind: "primary" },
]
const INDEX_THREE_RULES = makeIndex(THREE_RULES)

// ── Helper de render ──────────────────────────────────────────────────────────

function renderTable(
  items: DriftEntry[],
  overrides: Partial<React.ComponentProps<typeof DriftTable>> = {},
) {
  return render(
    <MemoryRouter>
      <DriftTable
        items={items}
        total={items.length}
        pagination={PAGINATION}
        onPaginationChange={vi.fn()}
        onIgnore={vi.fn()}
        onMarkMapped={vi.fn()}
        onDelete={vi.fn()}
        mappings={[{ id: "def-sophos", vendor: "sophos", event_type: "detection" }]}
        {...overrides}
      />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUsePermission.mockReturnValue(false)
})

// ── Coluna "Mapeado por" ──────────────────────────────────────────────────────

describe('DriftTable — coluna "Mapeado por"', () => {
  it('cabeçalho da coluna "Mapeado por" está presente', () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    expect(screen.getByText("Mapeado por")).toBeInTheDocument()
  })

  it("mostra '—' quando count é 0 (sem regras)", () => {
    renderTable([ENTRY_ZERO], { fieldRulesIndex: INDEX_ZERO_RULES })
    // "—" como muted em vez de link clicável
    expect(screen.queryByTestId("rules-count-d2")).not.toBeInTheDocument()
    expect(screen.getByLabelText("Nenhuma regra consome este campo")).toBeInTheDocument()
  })

  it("mostra '1 regra' (singular) quando count é 1", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ONE_RULE })
    expect(screen.getByTestId("rules-count-d1")).toHaveTextContent("1 regra")
  })

  it("mostra '3 regras' (plural) quando count é 3", () => {
    const index = makeIndex(THREE_RULES)
    renderTable([ENTRY_NEW], { fieldRulesIndex: index })
    expect(screen.getByTestId("rules-count-d1")).toHaveTextContent("3 regras")
  })

  it("mostra '…' quando fieldRulesIndex é null (carregando)", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: null })
    expect(screen.getByLabelText("Carregando regras...")).toBeInTheDocument()
  })

  it("mostra '…' quando fieldRulesIndex é undefined (não passado)", () => {
    renderTable([ENTRY_NEW])
    expect(screen.getByLabelText("Carregando regras...")).toBeInTheDocument()
  })
})

// ── Clique na contagem ────────────────────────────────────────────────────────

describe("DriftTable — clique na contagem de regras", () => {
  it("clique em '1 regra' dispara onOpenRulesDrawer com a entry e as regras", () => {
    const onOpenRulesDrawer = vi.fn()
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ONE_RULE, onOpenRulesDrawer })

    fireEvent.click(screen.getByTestId("rules-count-d1"))

    expect(onOpenRulesDrawer).toHaveBeenCalledTimes(1)
    const [entry, rules] = onOpenRulesDrawer.mock.calls[0]
    expect(entry).toMatchObject({ id: "d1", field_path: "threat.actor" })
    expect(rules).toHaveLength(1)
    expect(rules[0].match_kind).toBe("primary")
  })

  it("'—' não é clicável (sem button para zero regras)", () => {
    const onOpenRulesDrawer = vi.fn()
    renderTable([ENTRY_ZERO], { fieldRulesIndex: INDEX_ZERO_RULES, onOpenRulesDrawer })

    // O elemento "—" existe mas não é um button
    expect(screen.queryByTestId("rules-count-d2")).not.toBeInTheDocument()
    expect(onOpenRulesDrawer).not.toHaveBeenCalled()
  })

  it("button tem aria-label descritivo com singular", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ONE_RULE })
    const btn = screen.getByTestId("rules-count-d1")
    expect(btn).toHaveAttribute(
      "aria-label",
      "1 regra consomem este campo, clique para ver detalhes",
    )
  })

  it("button tem aria-label com plural quando 3 regras", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_THREE_RULES })
    const btn = screen.getByTestId("rules-count-d1")
    expect(btn).toHaveAttribute(
      "aria-label",
      "3 regras consomem este campo, clique para ver detalhes",
    )
  })
})

// ── Responsividade das colunas ────────────────────────────────────────────────

describe("DriftTable — column hiding responsive classes", () => {
  it("cabeçalho 'Última vez' tem classe hidden lg:table-cell", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const th = screen.getByRole("columnheader", { name: /última vez/i })
    expect(th.className).toMatch(/hidden/)
    expect(th.className).toMatch(/lg:table-cell/)
  })

  it("cabeçalho 'Tipo' (sample_type) tem classe hidden lg:table-cell", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const th = screen.getByRole("columnheader", { name: /^tipo$/i })
    expect(th.className).toMatch(/hidden/)
    expect(th.className).toMatch(/lg:table-cell/)
  })

  it("cabeçalho 'Ocorrências' tem classe hidden lg:table-cell", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const th = screen.getByRole("columnheader", { name: /ocorrências/i })
    expect(th.className).toMatch(/hidden/)
    expect(th.className).toMatch(/lg:table-cell/)
  })

  it("cabeçalho 'Tipo de Evento' tem classe hidden md:table-cell", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const th = screen.getByRole("columnheader", { name: /tipo de evento/i })
    expect(th.className).toMatch(/hidden/)
    expect(th.className).toMatch(/md:table-cell/)
  })

  it("cabeçalho 'Vendor' não tem classe hidden (sempre visível)", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const th = screen.getByRole("columnheader", { name: /^vendor$/i })
    expect(th.className).not.toMatch(/\bhidden\b/)
  })

  it("cabeçalho 'Campo' não tem classe hidden (sempre visível)", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const th = screen.getByRole("columnheader", { name: /^campo$/i })
    expect(th.className).not.toMatch(/\bhidden\b/)
  })
})

// ── Ações existentes preservadas ──────────────────────────────────────────────

describe("DriftTable — ações existentes não quebradas", () => {
  it("botão Ignorar aparece com permissão drift.ignore", () => {
    mockedUsePermission.mockImplementation((p) => p === "drift.ignore")
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    expect(screen.getByTestId("ignore-button-d1")).toBeInTheDocument()
  })

  it("botão Criar regra sempre aparece", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    expect(screen.getByRole("button", { name: /criar regra/i })).toBeInTheDocument()
  })

  it("múltiplos itens: cada um tem seu próprio rules-count button ou '—'", () => {
    renderTable([ENTRY_NEW, ENTRY_ZERO], { fieldRulesIndex: INDEX_ONE_RULE })
    // ENTRY_NEW tem source match
    expect(screen.getByTestId("rules-count-d1")).toBeInTheDocument()
    // ENTRY_ZERO não tem source match
    expect(screen.queryByTestId("rules-count-d2")).not.toBeInTheDocument()
  })
})

// ── Seleção (checkbox) ────────────────────────────────────────────────────────

describe("DriftTable — checkboxes de seleção", () => {
  it("checkbox por linha é renderizado com data-testid correto", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    expect(screen.getByTestId("drift-select-d1")).toBeInTheDocument()
  })

  it("checkbox de header 'select all' é renderizado", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    expect(screen.getByTestId("drift-select-all")).toBeInTheDocument()
  })

  it("clicar no checkbox de linha chama onToggleOne com o id", () => {
    const onToggleOne = vi.fn()
    renderTable([ENTRY_NEW], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(),
      onToggleOne,
    })
    fireEvent.click(screen.getByTestId("drift-select-d1"))
    expect(onToggleOne).toHaveBeenCalledWith("d1")
  })

  it("clicar no checkbox de linha quando já selecionado também chama onToggleOne (toggle)", () => {
    const onToggleOne = vi.fn()
    renderTable([ENTRY_NEW], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(["d1"]),
      onToggleOne,
    })
    fireEvent.click(screen.getByTestId("drift-select-d1"))
    expect(onToggleOne).toHaveBeenCalledWith("d1")
  })

  it("checkbox de linha reflete estado de seleção passado via prop", () => {
    renderTable([ENTRY_NEW], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(["d1"]),
    })
    expect(screen.getByTestId("drift-select-d1")).toBeChecked()
  })

  it("clicar no 'select all' chama onToggleAllVisible", () => {
    const onToggleAllVisible = vi.fn()
    renderTable([ENTRY_NEW, ENTRY_ZERO], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(),
      onToggleAllVisible,
    })
    fireEvent.click(screen.getByTestId("drift-select-all"))
    expect(onToggleAllVisible).toHaveBeenCalledTimes(1)
  })

  it("header reflete headerCheckboxState='checked' quando passado", () => {
    renderTable([ENTRY_NEW, ENTRY_ZERO], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(["d1", "d2"]),
      headerCheckboxState: "checked",
    })
    const headerCb = screen.getByTestId("drift-select-all") as HTMLInputElement
    expect(headerCb.checked).toBe(true)
    expect(headerCb.indeterminate).toBe(false)
  })

  it("header reflete headerCheckboxState='indeterminate' quando passado", () => {
    renderTable([ENTRY_NEW, ENTRY_ZERO], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(["d1"]),
      headerCheckboxState: "indeterminate",
    })
    const headerCb = screen.getByTestId("drift-select-all") as HTMLInputElement
    expect(headerCb.indeterminate).toBe(true)
  })

  it("fallback: deriva header state da selection quando headerCheckboxState ausente", () => {
    renderTable([ENTRY_NEW, ENTRY_ZERO], {
      fieldRulesIndex: INDEX_ZERO_RULES,
      selection: new Set<string>(["d1", "d2"]),
    })
    const headerCb = screen.getByTestId("drift-select-all") as HTMLInputElement
    expect(headerCb.checked).toBe(true)
  })

  it("checkbox de linha tem aria-label com o field_path", () => {
    renderTable([ENTRY_NEW], { fieldRulesIndex: INDEX_ZERO_RULES })
    const cb = screen.getByTestId("drift-select-d1")
    expect(cb).toHaveAttribute("aria-label", `Selecionar ${ENTRY_NEW.field_path}`)
  })
})
