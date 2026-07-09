/**
 * Testes de QuarantinePage — PR #3 bulk operations.
 *
 * Cobre:
 * - "Selecionar tudo do filtro" chama listQuarantineIds e popula seleção.
 * - BulkActionBar aparece com selectedCount e dispara modais de confirm.
 * - runDiscard chama bulkDiscardQuarantine em batches alinhados ao cap.
 * - runReprocess chama bulkReprocessQuarantine.
 * - Confirmação textual exigida quando seleção > 10.
 * - Sem permissão quarantine.discard → controles bulk não aparecem.
 */

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import QuarantinePage from "@/pages/QuarantinePage"
import * as quarantineHooks from "@/hooks/useQuarantine"
import * as permHooks from "@/hooks/usePermission"
import * as apiModule from "@/services/api"
import type { QuarantineDetail, QuarantineEntry } from "@/types"
import i18n from "@/i18n"

vi.mock("@/hooks/useQuarantine")
vi.mock("@/hooks/usePermission")
vi.mock("@/services/api", async () => {
  // Preserva exports reais (constantes e demais funções) e mocka apenas
  // as chamadas bulk pra evitar requisição de rede nos testes.
  const actual = await vi.importActual<typeof import("@/services/api")>(
    "@/services/api",
  )
  return {
    ...actual,
    bulkDiscardQuarantine: vi.fn(),
    bulkReprocessQuarantine: vi.fn(),
    listQuarantineIds: vi.fn(),
  }
})

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, json: async () => [] }),
  )
})

afterAll(() => {
  vi.unstubAllGlobals()
})

const mockedUseQuarantine = vi.mocked(quarantineHooks.useQuarantine)
const mockedUsePermission = vi.mocked(permHooks.usePermission)
const mockedBulkDiscard = vi.mocked(apiModule.bulkDiscardQuarantine)
const mockedBulkReprocess = vi.mocked(apiModule.bulkReprocessQuarantine)
const mockedListIds = vi.mocked(apiModule.listQuarantineIds)

const FUTURE_DATE = "2099-01-01T00:00:00Z"

function makeEntry(id: string): QuarantineEntry {
  return {
    id,
    integration_id: 1,
    vendor: "sophos",
    event_type: "endpoint.threat",
    error_kind: "schema_error",
    error_detail: "x",
    mapping_version_id: "mv1",
    created_at: "2026-01-01T00:00:00Z",
    expires_at: FUTURE_DATE,
    reprocessed_at: null,
  }
}

const DETAIL: QuarantineDetail = { ...makeEntry("q1"), raw_payload: {} }

function defaultHook(items: QuarantineEntry[]) {
  return {
    items,
    total: items.length,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
    discard: vi.fn(),
    reprocess: vi.fn(),
    getDetail: vi.fn().mockResolvedValue(DETAIL),
  }
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/quarantine"]}>
      <Routes>
        <Route path="/quarantine" element={<QuarantinePage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseQuarantine.mockReturnValue(
    defaultHook([makeEntry("q1"), makeEntry("q2"), makeEntry("q3")]),
  )
  // Permission default: tem QUARANTINE_DISCARD (operator).
  mockedUsePermission.mockImplementation((p) => p === "quarantine.discard")
  mockedBulkDiscard.mockResolvedValue({
    processed: 0,
    discarded: 0,
    errors: [],
  })
  mockedBulkReprocess.mockResolvedValue({
    accepted: 0,
    expired: 0,
    already_reprocessed: 0,
    errors: [],
  })
  mockedListIds.mockResolvedValue({ total: 0, ids: [], capped: false })
})

describe("QuarantinePage bulk (PR #3)", () => {
  it("não exibe controles bulk se user não tem quarantine.discard", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId("quarantine-select-all-filter")).toBeNull()
    expect(screen.queryByTestId("quarantine-bulk-header-checkbox")).toBeNull()
  })

  it("exibe botão 'Selecionar tudo do filtro' quando há itens + permissão", () => {
    renderPage()
    expect(
      screen.getByTestId("quarantine-select-all-filter"),
    ).toBeInTheDocument()
  })

  it("seleção via checkbox de linha exibe BulkActionBar", () => {
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q1"))
    expect(
      screen.getByTestId("quarantine-bulk-action-bar"),
    ).toBeInTheDocument()
    expect(screen.getByText("1 entrada(s) selecionado(s)")).toBeInTheDocument()
  })

  it("'Selecionar tudo do filtro' popula a seleção via listQuarantineIds", async () => {
    mockedListIds.mockResolvedValue({
      total: 3,
      ids: ["q1", "q2", "q3"],
      capped: false,
    })
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-select-all-filter"))
    await waitFor(() => {
      expect(mockedListIds).toHaveBeenCalledTimes(1)
    })
    await waitFor(() => {
      expect(
        screen.getByText("3 entrada(s) selecionado(s)"),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByTestId("quarantine-select-all-notice"),
    ).toHaveTextContent("3 itens selecionados")
  })

  it("mostra warning quando capped=true em listQuarantineIds", async () => {
    mockedListIds.mockResolvedValue({
      total: 5000,
      ids: Array.from({ length: 2000 }, (_, i) => `id-${i}`),
      capped: true,
    })
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-select-all-filter"))
    await waitFor(() => {
      expect(
        screen.getByTestId("quarantine-select-all-notice"),
      ).toHaveTextContent(/2000 itens mais recentes \(de 5000\)/)
    })
  })

  it("clicar em 'Descartar selecionados' (≤10) abre dialog SEM input de typing", async () => {
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q1"))
    fireEvent.click(screen.getByTestId("quarantine-bulk-discard-btn"))
    expect(
      screen.getByTestId("quarantine-bulk-discard-dialog"),
    ).toBeInTheDocument()
    expect(
      screen.queryByTestId("quarantine-bulk-discard-typed"),
    ).toBeNull()
    // Botão de confirmar habilitado
    expect(
      screen.getByTestId("quarantine-bulk-discard-dialog-confirm"),
    ).not.toBeDisabled()
  })

  it("descarte com >10 itens: dialog mostra typing input e botão fica disabled até digitar 'DESCARTAR'", async () => {
    // Hook com 11 itens visíveis.
    mockedUseQuarantine.mockReturnValue(
      defaultHook(
        Array.from({ length: 11 }, (_, i) => makeEntry(`q${i + 1}`)),
      ),
    )
    renderPage()
    // Seleciona todos visíveis via header checkbox.
    fireEvent.click(screen.getByTestId("quarantine-bulk-header-checkbox"))
    fireEvent.click(screen.getByTestId("quarantine-bulk-discard-btn"))

    expect(
      screen.getByTestId("quarantine-bulk-discard-typed"),
    ).toBeInTheDocument()

    const confirmBtn = screen.getByTestId(
      "quarantine-bulk-discard-dialog-confirm",
    )
    expect(confirmBtn).toBeDisabled()

    // Digita errado — segue desabilitado.
    const input = screen.getByLabelText(
      "Confirmação textual de descarte em massa",
    )
    fireEvent.change(input, { target: { value: "descartar" } })
    expect(confirmBtn).toBeDisabled()

    // Digita certo — habilita.
    fireEvent.change(input, { target: { value: "DESCARTAR" } })
    expect(confirmBtn).not.toBeDisabled()
  })

  it("confirmar descarte chama bulkDiscardQuarantine em batches", async () => {
    mockedBulkDiscard.mockResolvedValue({
      processed: 2,
      discarded: 2,
      errors: [],
    })
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q1"))
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q2"))
    fireEvent.click(screen.getByTestId("quarantine-bulk-discard-btn"))

    await act(async () => {
      fireEvent.click(
        screen.getByTestId("quarantine-bulk-discard-dialog-confirm"),
      )
    })

    await waitFor(() => {
      expect(mockedBulkDiscard).toHaveBeenCalledTimes(1)
    })
    expect(mockedBulkDiscard).toHaveBeenCalledWith(
      expect.arrayContaining(["q1", "q2"]),
    )
    await waitFor(() =>
      expect(
        screen.getByTestId("quarantine-bulk-notice"),
      ).toHaveTextContent(/2 entradas descartadas/),
    )
  })

  it("confirmar reprocesso chama bulkReprocessQuarantine e exibe sumario", async () => {
    mockedBulkReprocess.mockResolvedValue({
      accepted: 2,
      expired: 0,
      already_reprocessed: 0,
      errors: [],
    })
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q1"))
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q2"))
    fireEvent.click(screen.getByTestId("quarantine-bulk-reprocess-btn"))

    expect(
      screen.getByTestId("quarantine-bulk-reprocess-dialog"),
    ).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(
        screen.getByTestId("quarantine-bulk-reprocess-dialog-confirm"),
      )
    })

    await waitFor(() => {
      expect(mockedBulkReprocess).toHaveBeenCalledTimes(1)
    })
    expect(mockedBulkReprocess).toHaveBeenCalledWith(
      expect.arrayContaining(["q1", "q2"]),
    )
    await waitFor(() =>
      expect(
        screen.getByTestId("quarantine-bulk-notice"),
      ).toHaveTextContent(/2 aceitos/),
    )
  })

  it("clear na BulkActionBar limpa seleção e esconde a barra", () => {
    renderPage()
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q1"))
    expect(
      screen.getByTestId("quarantine-bulk-action-bar"),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByTestId("quarantine-bulk-action-bar-clear"))
    expect(
      screen.queryByTestId("quarantine-bulk-action-bar"),
    ).toBeNull()
  })
})

describe("QuarantinePage filtros novos (PR #3)", () => {
  it("renderiza dropdown de status (filter-status)", () => {
    renderPage()
    expect(screen.getByTestId("filter-status")).toBeInTheDocument()
  })

  it("renderiza search de integração (parte do FiltersToolbar)", () => {
    renderPage()
    expect(screen.getByLabelText("Buscar por nome da integração")).toBeInTheDocument()
  })
})
