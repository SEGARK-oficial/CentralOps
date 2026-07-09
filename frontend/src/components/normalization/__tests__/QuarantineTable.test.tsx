/**
 * Testes de QuarantineTable — F4-S3
 * Cobre: render, permissão, expirado, reprocessado, ConfirmDialog, sucesso, erros.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { QuarantineTable } from "@/components/normalization/QuarantineTable"
import * as permHooks from "@/hooks/usePermission"
import type { PaginationConfig, QuarantineDetail, QuarantineEntry } from "@/types"

vi.mock("@/hooks/usePermission")

const mockedUsePermission = vi.mocked(permHooks.usePermission)

const FUTURE_DATE = "2099-01-01T00:00:00Z"
const PAST_DATE = "2020-01-01T00:00:00Z"

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

const ENTRY_REPROCESSED: QuarantineEntry = {
  ...ENTRY,
  id: "q2",
  reprocessed_at: "2026-04-25T10:00:00Z",
}

const ENTRY_EXPIRED: QuarantineEntry = {
  ...ENTRY,
  id: "q3",
  expires_at: PAST_DATE,
}

const PAGINATION: PaginationConfig = {
  current: 1,
  pageSize: 20,
  showTotal: true,
  showSizeChanger: true,
}

const DETAIL: QuarantineDetail = {
  ...ENTRY,
  raw_payload: { event: "endpoint.threat" },
}

function renderTable(
  items: QuarantineEntry[] = [ENTRY],
  overrides: Partial<React.ComponentProps<typeof QuarantineTable>> = {},
) {
  return render(
    <MemoryRouter>
      <QuarantineTable
        items={items}
        total={items.length}
        pagination={PAGINATION}
        onPaginationChange={vi.fn()}
        onDiscard={vi.fn()}
        onReprocess={vi.fn()}
        onGetDetail={vi.fn().mockResolvedValue(DETAIL)}
        onOpenDetail={vi.fn()}
        {...overrides}
      />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUsePermission.mockReturnValue(false)
})

describe("QuarantineTable", () => {
  it("renderiza linha com vendor e error_kind", () => {
    renderTable()
    expect(screen.getByText("sophos")).toBeInTheDocument()
    expect(screen.getByText("schema_error")).toBeInTheDocument()
  })

  it("Reprocessar visível para user com permissão quarantine.discard", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderTable()
    expect(screen.getByTestId(`reprocess-button-${ENTRY.id}`)).toBeInTheDocument()
  })

  it("Reprocessar NÃO aparece sem permissão", () => {
    mockedUsePermission.mockReturnValue(false)
    renderTable()
    expect(screen.queryByTestId(`reprocess-button-${ENTRY.id}`)).not.toBeInTheDocument()
  })

  it("Reprocessar abre ConfirmDialog antes de chamar API", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onReprocess = vi.fn()
    renderTable([ENTRY], { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${ENTRY.id}`))

    expect(screen.getByText("Reprocessar evento?")).toBeInTheDocument()
    expect(onReprocess).not.toHaveBeenCalled()
  })

  it("cancelar ConfirmDialog não chama onReprocess", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onReprocess = vi.fn()
    renderTable([ENTRY], { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${ENTRY.id}`))
    fireEvent.click(screen.getByRole("button", { name: /cancelar/i }))

    expect(onReprocess).not.toHaveBeenCalled()
  })

  it("confirmar reprocess chama onReprocess com id", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onReprocess = vi.fn().mockResolvedValue({ ...ENTRY, reprocessed_at: "2026-04-25T10:00:00Z" })
    renderTable([ENTRY], { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${ENTRY.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^reprocessar$/i }))

    await waitFor(() => expect(onReprocess).toHaveBeenCalledWith(ENTRY.id))
  })

  it("Reprocess success mostra Notice de sucesso", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onReprocess = vi.fn().mockResolvedValue({ ...ENTRY, reprocessed_at: "2026-04-25T10:00:00Z" })
    renderTable([ENTRY], { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${ENTRY.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^reprocessar$/i }))

    await waitFor(() => {
      expect(screen.getByTestId("reprocess-success-notice")).toBeInTheDocument()
    })
  })

  it("Reprocess 422 mostra error_detail do backend", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const err = Object.assign(new Error("Mapping ainda falha: rule required"), { statusCode: 422 })
    const onReprocess = vi.fn().mockRejectedValue(err)
    renderTable([ENTRY], { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${ENTRY.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^reprocessar$/i }))

    await waitFor(() => {
      expect(screen.getByTestId("reprocess-error-notice")).toBeInTheDocument()
    })
    expect(screen.getByText("Mapping ainda falha: rule required")).toBeInTheDocument()
  })

  it("Já reprocessado mostra Badge em vez de botão", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderTable([ENTRY_REPROCESSED])
    expect(screen.queryByTestId(`reprocess-button-${ENTRY_REPROCESSED.id}`)).not.toBeInTheDocument()
    // Badge Reprocessado aparece tanto na coluna status quanto na coluna actions
    const badges = screen.getAllByText("Reprocessado")
    expect(badges.length).toBeGreaterThanOrEqual(1)
  })

  it("Expirado mostra Badge Expirado em vez de botão", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderTable([ENTRY_EXPIRED])
    expect(screen.queryByTestId(`reprocess-button-${ENTRY_EXPIRED.id}`)).not.toBeInTheDocument()
    expect(screen.getByText("Expirado")).toBeInTheDocument()
  })

  it("botão Descartar aparece com permissão quarantine.discard", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderTable()
    expect(screen.getByTestId(`discard-button-${ENTRY.id}`)).toBeInTheDocument()
  })

  it("botão Descartar NÃO aparece sem permissão", () => {
    mockedUsePermission.mockReturnValue(false)
    renderTable()
    expect(screen.queryByTestId(`discard-button-${ENTRY.id}`)).not.toBeInTheDocument()
  })

  it("clicar em Descartar abre ConfirmDialog", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderTable()

    fireEvent.click(screen.getByTestId(`discard-button-${ENTRY.id}`))
    expect(screen.getByText("Descartar entrada?")).toBeInTheDocument()
  })

  it("confirmar Descartar chama onDiscard com id", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onDiscard = vi.fn().mockResolvedValue(undefined)
    renderTable([ENTRY], { onDiscard })

    fireEvent.click(screen.getByTestId(`discard-button-${ENTRY.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^descartar$/i }))

    await waitFor(() => expect(onDiscard).toHaveBeenCalledWith(ENTRY.id))
  })

  it("clicar em Detalhes chama onGetDetail e onOpenDetail", async () => {
    const onGetDetail = vi.fn().mockResolvedValue(DETAIL)
    const onOpenDetail = vi.fn()
    renderTable([ENTRY], { onGetDetail, onOpenDetail })

    fireEvent.click(screen.getByRole("button", { name: /ver detalhes/i }))

    await waitFor(() => {
      expect(onGetDetail).toHaveBeenCalledWith(ENTRY.id)
      expect(onOpenDetail).toHaveBeenCalledWith(DETAIL)
    })
  })

  // ── Paginação server-side ───────────────────────────────────────────────────
  // Regressão: antes do fix, DataTable rodava em modo client-side e fazia
  // slice((page-1)*pageSize, page*pageSize) sobre os 20 items que o backend já
  // tinha paginado para a página 2 → resultado: tabela vazia.
  // Com serverSide=true, a tabela renderiza diretamente os items recebidos.
  describe("paginação server-side", () => {
    function makeEntries(n: number, prefix: string): QuarantineEntry[] {
      return Array.from({ length: n }, (_, i) => ({
        ...ENTRY,
        id: `${prefix}-${i}`,
        error_detail: `Detalhe ${prefix}-${i}`,
      }))
    }

    it("renderiza items da página 2 quando backend já paginou (total=45, page=2)", () => {
      const pageTwoItems = makeEntries(20, "p2")
      renderTable(pageTwoItems, {
        total: 45,
        pagination: { ...PAGINATION, current: 2 },
      })

      // Todas as 20 linhas da página 2 devem estar no DOM
      expect(screen.getByText("Detalhe p2-0")).toBeInTheDocument()
      expect(screen.getByText("Detalhe p2-19")).toBeInTheDocument()

      // EmptyState não deve aparecer
      expect(
        screen.queryByText("Nenhuma entrada em quarentena."),
      ).not.toBeInTheDocument()

      // Total reflete o backend, não o length local
      expect(
        screen.getByText(/Mostrando 21 a 40 de 45 registros/i),
      ).toBeInTheDocument()
    })

    it("renderiza items da última página parcial (total=45, page=3, 5 items)", () => {
      const pageThreeItems = makeEntries(5, "p3")
      renderTable(pageThreeItems, {
        total: 45,
        pagination: { ...PAGINATION, current: 3 },
      })

      expect(screen.getByText("Detalhe p3-0")).toBeInTheDocument()
      expect(screen.getByText("Detalhe p3-4")).toBeInTheDocument()
      expect(
        screen.getByText(/Mostrando 41 a 45 de 45 registros/i),
      ).toBeInTheDocument()
    })

    it("clicar em próxima página chama onPaginationChange com current=2", () => {
      const onPaginationChange = vi.fn()
      const pageOneItems = makeEntries(20, "p1")
      renderTable(pageOneItems, {
        total: 45,
        pagination: { ...PAGINATION, current: 1 },
        onPaginationChange,
      })

      fireEvent.click(screen.getByRole("button", { name: /próxima página/i }))

      expect(onPaginationChange).toHaveBeenCalledWith(
        expect.objectContaining({ current: 2 }),
      )
    })
  })
})
