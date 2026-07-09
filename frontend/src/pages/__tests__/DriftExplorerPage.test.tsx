/**
 * Testes de DriftExplorerPage
 * Cobre: render filtros + tabela, gating de actions por role,
 *        navegação para editor de mapping, reset filtros.
 * Fase 4.3: coluna "Mapeado por" e drawer de regras.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import DriftExplorerPage from "@/pages/DriftExplorerPage"
import * as driftHooks from "@/hooks/useDrift"
import * as permHooks from "@/hooks/usePermission"
import * as fieldRulesHooks from "@/hooks/useFieldRules"
import type { DriftEntry } from "@/types"
import type { FieldRulesIndex, MatchedRule } from "@/hooks/useFieldRules"

// Mock hooks
vi.mock("@/hooks/useDrift")
vi.mock("@/hooks/usePermission")
vi.mock("@/hooks/useFieldRules")

// Mock the API for summary calls
vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listDrift: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 }),
  }
})

const mockedUseDrift = vi.mocked(driftHooks.useDrift)
const mockedUsePermission = vi.mocked(permHooks.usePermission)
const mockedUseFieldRules = vi.mocked(fieldRulesHooks.useFieldRules)

// Suppress fetch errors from mapping fetch in tests (no server)
beforeAll(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, json: async () => [] }))
})

afterAll(() => {
  vi.unstubAllGlobals()
})

// ── FieldRulesIndex helpers ───────────────────────────────────────────────────

function makeIndex(rules: MatchedRule[]): FieldRulesIndex {
  return {
    lookup: (_v: string, _et: string, path: string) =>
      rules.filter((r) => r.source === path || path.startsWith(r.source + ".") || r.source.startsWith(path + ".")),
    count: function(v, et, path) { return this.lookup(v, et, path).length },
  }
}

const MATCHED_RULE: MatchedRule = {
  rule_target: "normalized.custom",
  source: "extra.custom",
  match_kind: "primary",
  mapping_definition_id: "def-wazuh-001",
  vendor: "wazuh",
  event_type: "authentication",
}

const ENTRY: DriftEntry = {
  id: "d1",
  vendor: "wazuh",
  event_type: "authentication",
  field_path: "extra.custom",
  sample_value: "hello",
  sample_type: "string",
  occurrence_count: 10,
  first_seen: "2026-01-01T00:00:00Z",
  last_seen: "2026-01-02T00:00:00Z",
  status: "new",
}

const HOOK_DEFAULT: ReturnType<typeof driftHooks.useDrift> = {
  items: [ENTRY],
  total: 1,
  isLoading: false,
  error: null,
  refetch: vi.fn(),
  ignoreField: vi.fn(),
  markMapped: vi.fn(),
  deleteField: vi.fn(),
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/drift"]}>
      <Routes>
        <Route path="/drift" element={<DriftExplorerPage />} />
        <Route path="/mappings/:id" element={<div>Mapping Editor</div>} />
        <Route path="/mappings" element={<div>Mappings List</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseDrift.mockReturnValue(HOOK_DEFAULT)
  // Default: nenhuma permissão
  mockedUsePermission.mockReturnValue(false)
  // Default: índice carregando
  mockedUseFieldRules.mockReturnValue({ data: null, loading: true, error: null })
})

describe("DriftExplorerPage", () => {
  it("renderiza o page header com título correto", () => {
    renderPage()
    expect(screen.getByText("Drift Explorer")).toBeInTheDocument()
    expect(screen.getByText("Campos do raw que nenhum mapping consome")).toBeInTheDocument()
  })

  it("renderiza a barra de filtros", () => {
    renderPage()
    expect(screen.getByTestId("filter-vendor")).toBeInTheDocument()
    expect(screen.getByTestId("filter-event-type")).toBeInTheDocument()
    expect(screen.getByTestId("filter-status")).toBeInTheDocument()
  })

  it("renderiza a tabela com dados", () => {
    renderPage()
    expect(screen.getByTestId("drift-table")).toBeInTheDocument()
    expect(screen.getByText("extra.custom")).toBeInTheDocument()
  })

  it("exibe LoadingSpinner quando isLoading=true", () => {
    mockedUseDrift.mockReturnValue({
      ...HOOK_DEFAULT,
      items: [],
      isLoading: true,
    })

    renderPage()
    expect(screen.getByText("Carregando dados de drift...")).toBeInTheDocument()
  })

  it("exibe Notice de erro com botão retry quando há erro", () => {
    mockedUseDrift.mockReturnValue({
      ...HOOK_DEFAULT,
      items: [],
      error: new Error("Falha de rede"),
    })

    renderPage()
    expect(screen.getByText("Erro ao carregar dados de drift")).toBeInTheDocument()
    expect(screen.getByText("Falha de rede")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
  })

  it("botão Ignorar NÃO aparece sem permissão drift.ignore", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId("ignore-button-d1")).not.toBeInTheDocument()
  })

  it("botão Ignorar aparece com permissão drift.ignore", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "drift.ignore")
    renderPage()
    expect(screen.getByTestId("ignore-button-d1")).toBeInTheDocument()
  })

  it("botão Marcar Mapeado NÃO aparece sem permissão drift.mark_mapped", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId("mark-mapped-button-d1")).not.toBeInTheDocument()
  })

  it("botão Marcar Mapeado aparece com permissão drift.mark_mapped", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "drift.mark_mapped")
    renderPage()
    expect(screen.getByTestId("mark-mapped-button-d1")).toBeInTheDocument()
  })

  it("botão Remover NÃO aparece sem permissão drift.delete", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId("delete-button-d1")).not.toBeInTheDocument()
  })

  it("botão Remover aparece com permissão drift.delete", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "drift.delete")
    renderPage()
    expect(screen.getByTestId("delete-button-d1")).toBeInTheDocument()
  })

  it("botão Criar regra sempre aparece", () => {
    renderPage()
    expect(screen.getByRole("button", { name: /criar regra/i })).toBeInTheDocument()
  })

  it("clicar em Ignorar abre ConfirmDialog", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "drift.ignore")
    renderPage()

    fireEvent.click(screen.getByTestId("ignore-button-d1"))

    expect(screen.getByText("Ignorar campo?")).toBeInTheDocument()
  })

  it("clicar em Remover abre ConfirmDialog de danger", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "drift.delete")
    renderPage()

    fireEvent.click(screen.getByTestId("delete-button-d1"))

    expect(screen.getByText("Remover entrada?")).toBeInTheDocument()
  })

  it("confirmar Ignorar chama ignoreField", async () => {
    const ignoreField = vi.fn().mockResolvedValue(undefined)
    mockedUseDrift.mockReturnValue({ ...HOOK_DEFAULT, ignoreField })
    mockedUsePermission.mockImplementation((perm) => perm === "drift.ignore")

    renderPage()
    fireEvent.click(screen.getByTestId("ignore-button-d1"))

    // ConfirmDialog has two buttons: Cancelar and Ignorar.
    // Find the confirm button inside the modal (after the dialog title is visible).
    await waitFor(() => expect(screen.getByText("Ignorar campo?")).toBeInTheDocument())
    // The modal confirm button says "Ignorar" and is not the table row button
    const buttons = screen.getAllByRole("button", { name: /^ignorar$/i })
    // The last button is the confirm one inside the ConfirmDialog footer
    fireEvent.click(buttons[buttons.length - 1])

    await waitFor(() => expect(ignoreField).toHaveBeenCalledWith("d1"))
  })

  it("confirmar Remover chama deleteField", async () => {
    const deleteField = vi.fn().mockResolvedValue(undefined)
    mockedUseDrift.mockReturnValue({ ...HOOK_DEFAULT, deleteField })
    mockedUsePermission.mockImplementation((perm) => perm === "drift.delete")

    renderPage()
    fireEvent.click(screen.getByTestId("delete-button-d1"))
    // The ConfirmDialog has multiple "Remover" buttons — use the confirm one inside the dialog
    const buttons = screen.getAllByRole("button", { name: /remover/i })
    // Last one is in ConfirmDialog
    fireEvent.click(buttons[buttons.length - 1])

    await waitFor(() => expect(deleteField).toHaveBeenCalledWith("d1"))
  })

  it("grupo de status: clicar em 'Novos' atualiza filtros", () => {
    renderPage()

    const novosButton = screen.getByRole("button", { name: /novos/i })
    fireEvent.click(novosButton)

    // useDrift should be called with status: "new" on next render
    // The hook is re-rendered with new filters
    expect(mockedUseDrift).toHaveBeenCalledWith(
      expect.objectContaining({ status: "new" }),
    )
  })

  it("botão resetar filtros aparece quando há filtro ativo", async () => {
    renderPage()

    const novosButton = screen.getByRole("button", { name: /novos/i })
    fireEvent.click(novosButton)

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /resetar filtros/i })).toBeInTheDocument(),
    )
  })

  it("empty state quando items vazio", () => {
    mockedUseDrift.mockReturnValue({ ...HOOK_DEFAULT, items: [], total: 0 })

    renderPage()
    expect(screen.getByText("Nenhum campo de drift encontrado.")).toBeInTheDocument()
  })
})

// ── Fase 4.3: coluna Mapeado por + drawer ────────────────────────────────────

describe("DriftExplorerPage — Fase 4.3 cross-reference", () => {
  it("mostra '…' enquanto useFieldRules está carregando (loading=true)", () => {
    mockedUseFieldRules.mockReturnValue({ data: null, loading: true, error: null })
    renderPage()
    expect(screen.getByLabelText("Carregando regras...")).toBeInTheDocument()
  })

  it("mostra '—' quando índice pronto mas sem regras para o campo", () => {
    const emptyIndex = makeIndex([])
    mockedUseFieldRules.mockReturnValue({ data: emptyIndex, loading: false, error: null })
    renderPage()
    expect(screen.getByLabelText("Nenhuma regra consome este campo")).toBeInTheDocument()
  })

  it("mostra contagem clicável quando há regras para o campo", () => {
    const index = makeIndex([MATCHED_RULE])
    mockedUseFieldRules.mockReturnValue({ data: index, loading: false, error: null })
    renderPage()
    // ENTRY.field_path = "extra.custom" que corresponde ao MATCHED_RULE.source
    expect(screen.getByTestId("rules-count-d1")).toBeInTheDocument()
    expect(screen.getByTestId("rules-count-d1")).toHaveTextContent("1 regra")
  })

  it("clicar na contagem abre o drawer com o field_path correto no heading", async () => {
    const index = makeIndex([MATCHED_RULE])
    mockedUseFieldRules.mockReturnValue({ data: index, loading: false, error: null })
    renderPage()

    fireEvent.click(screen.getByTestId("rules-count-d1"))

    await waitFor(() => {
      expect(screen.getByRole("dialog")).toBeInTheDocument()
    })
    // O drawer exibe o field_path no heading h2
    expect(screen.getByRole("heading", { name: "extra.custom" })).toBeInTheDocument()
  })

  it("drawer fecha ao clicar no botão ×", async () => {
    const index = makeIndex([MATCHED_RULE])
    mockedUseFieldRules.mockReturnValue({ data: index, loading: false, error: null })
    renderPage()

    fireEvent.click(screen.getByTestId("rules-count-d1"))
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("button", { name: /fechar drawer/i }))
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
  })

  it("drawer fecha com Escape", async () => {
    const index = makeIndex([MATCHED_RULE])
    mockedUseFieldRules.mockReturnValue({ data: index, loading: false, error: null })
    renderPage()

    fireEvent.click(screen.getByTestId("rules-count-d1"))
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument())

    fireEvent.keyDown(document, { key: "Escape" })
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
  })

  it("drawer mostra a regra matched com badge do match_kind", async () => {
    const index = makeIndex([MATCHED_RULE])
    mockedUseFieldRules.mockReturnValue({ data: index, loading: false, error: null })
    renderPage()

    fireEvent.click(screen.getByTestId("rules-count-d1"))
    await waitFor(() => expect(screen.getByRole("dialog")).toBeInTheDocument())

    expect(screen.getByTestId("drawer-rule-kind-0")).toHaveTextContent("Primário")
    expect(screen.getByTestId("drawer-rule-link-0")).toHaveAttribute(
      "href",
      "/mappings/def-wazuh-001",
    )
  })
})
