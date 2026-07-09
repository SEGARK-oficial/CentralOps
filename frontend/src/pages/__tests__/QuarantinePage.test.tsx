/**
 * Testes de QuarantinePage
 * Cobre: render filtros + tabela, abre drawer no Detalhes,
 *        descart com confirmação, reprocessar habilitado (F4-S3).
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import QuarantinePage from "@/pages/QuarantinePage"
import * as quarantineHooks from "@/hooks/useQuarantine"
import * as permHooks from "@/hooks/usePermission"
import type { QuarantineDetail, QuarantineEntry } from "@/types"
import i18n from "@/i18n"

vi.mock("@/hooks/useQuarantine")
vi.mock("@/hooks/usePermission")

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, json: async () => [] }))
})

afterAll(() => {
  vi.unstubAllGlobals()
})

const mockedUseQuarantine = vi.mocked(quarantineHooks.useQuarantine)
const mockedUsePermission = vi.mocked(permHooks.usePermission)

const FUTURE_DATE = "2099-01-01T00:00:00Z"

const ENTRY: QuarantineEntry = {
  id: "q1",
  integration_id: 1,
  vendor: "sophos",
  event_type: "endpoint.threat",
  error_kind: "schema_error",
  error_detail: "Field 'user' is required",
  mapping_version_id: "mv1",
  created_at: "2026-01-01T00:00:00Z",
  expires_at: FUTURE_DATE,
  reprocessed_at: null,
}

const DETAIL: QuarantineDetail = {
  ...ENTRY,
  raw_payload: { event: "endpoint.threat", data: { user: null } },
}

const HOOK_DEFAULT: ReturnType<typeof quarantineHooks.useQuarantine> = {
  items: [ENTRY],
  total: 1,
  isLoading: false,
  error: null,
  refetch: vi.fn(),
  discard: vi.fn(),
  reprocess: vi.fn(),
  getDetail: vi.fn().mockResolvedValue(DETAIL),
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/quarantine"]}>
      <Routes>
        <Route path="/quarantine" element={<QuarantinePage />} />
        <Route path="/mappings/:id" element={<div>Mapping Editor</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseQuarantine.mockReturnValue(HOOK_DEFAULT)
  mockedUsePermission.mockReturnValue(false)
})

describe("QuarantinePage", () => {
  it("renderiza o page header", () => {
    renderPage()
    expect(screen.getByText("Quarentena")).toBeInTheDocument()
    expect(screen.getByText("Eventos que falharam na normalização")).toBeInTheDocument()
  })

  it("renderiza a barra de filtros", () => {
    renderPage()
    expect(screen.getByTestId("filter-vendor")).toBeInTheDocument()
    expect(screen.getByTestId("filter-event-type")).toBeInTheDocument()
    expect(screen.getByTestId("filter-error-kind")).toBeInTheDocument()
  })

  it("renderiza a tabela com dados", () => {
    renderPage()
    expect(screen.getByTestId("quarantine-table")).toBeInTheDocument()
    // vendor aparece na tabela
    expect(screen.getByText("sophos")).toBeInTheDocument()
  })

  it("exibe LoadingSpinner quando isLoading=true", () => {
    mockedUseQuarantine.mockReturnValue({
      ...HOOK_DEFAULT,
      items: [],
      isLoading: true,
    })

    renderPage()
    expect(screen.getByText("Carregando quarentena...")).toBeInTheDocument()
  })

  it("exibe Notice de erro com retry", () => {
    mockedUseQuarantine.mockReturnValue({
      ...HOOK_DEFAULT,
      items: [],
      error: new Error("Falha de rede"),
    })

    renderPage()
    expect(screen.getByText("Erro ao carregar quarentena")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
  })

  it("clicar em Detalhes abre o drawer", async () => {
    const getDetail = vi.fn().mockResolvedValue(DETAIL)
    mockedUseQuarantine.mockReturnValue({ ...HOOK_DEFAULT, getDetail })

    renderPage()

    fireEvent.click(screen.getByRole("button", { name: /ver detalhes/i }))

    await waitFor(() => {
      expect(screen.getByTestId("quarantine-detail-drawer")).toBeInTheDocument()
    })
  })

  it("drawer mostra error_detail", async () => {
    const getDetail = vi.fn().mockResolvedValue(DETAIL)
    mockedUseQuarantine.mockReturnValue({ ...HOOK_DEFAULT, getDetail })

    renderPage()
    fireEvent.click(screen.getByRole("button", { name: /ver detalhes/i }))

    await waitFor(() => {
      expect(screen.getByText("Field 'user' is required")).toBeInTheDocument()
    })
  })

  it("botão Reprocessar no drawer aparece para user com permissão e entry não expirada", async () => {
    const getDetail = vi.fn().mockResolvedValue(DETAIL)
    mockedUseQuarantine.mockReturnValue({ ...HOOK_DEFAULT, getDetail })
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")

    renderPage()
    fireEvent.click(screen.getByRole("button", { name: /ver detalhes/i }))

    await waitFor(() => {
      expect(screen.getByTestId(`reprocess-button-${ENTRY.id}`)).toBeInTheDocument()
    })
  })

  it("botão Descartar NÃO aparece sem permissão quarantine.discard", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId(`discard-button-${ENTRY.id}`)).not.toBeInTheDocument()
  })

  it("botão Descartar aparece com permissão quarantine.discard", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderPage()
    expect(screen.getByTestId(`discard-button-${ENTRY.id}`)).toBeInTheDocument()
  })

  it("clicar em Descartar abre ConfirmDialog", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderPage()

    fireEvent.click(screen.getByTestId(`discard-button-${ENTRY.id}`))
    expect(screen.getByText("Descartar entrada?")).toBeInTheDocument()
  })

  it("confirmar Descartar chama discard", async () => {
    const discard = vi.fn().mockResolvedValue(undefined)
    mockedUseQuarantine.mockReturnValue({ ...HOOK_DEFAULT, discard })
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")

    renderPage()
    fireEvent.click(screen.getByTestId(`discard-button-${ENTRY.id}`))

    const confirmBtn = screen.getByRole("button", { name: /^descartar$/i })
    fireEvent.click(confirmBtn)

    await waitFor(() => expect(discard).toHaveBeenCalledWith(ENTRY.id))
  })

  it("botão Reprocessar na tabela aparece com permissão e entry não expirada", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderPage()
    expect(screen.getByTestId(`reprocess-button-${ENTRY.id}`)).toBeInTheDocument()
  })

  it("botão Reprocessar na tabela NÃO aparece sem permissão", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId(`reprocess-button-${ENTRY.id}`)).not.toBeInTheDocument()
  })

  it("empty state quando items vazio", () => {
    mockedUseQuarantine.mockReturnValue({ ...HOOK_DEFAULT, items: [], total: 0 })
    renderPage()
    expect(screen.getByText("Nenhuma entrada em quarentena.")).toBeInTheDocument()
  })
})
