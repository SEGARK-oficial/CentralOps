/**
 * Testes de QuarantineTable — PR #3 bulk select column
 * Cobre: coluna de checkbox aparece quando selection é passado, header
 *        tri-state, click em row toggles, ausência de selection mantém
 *        compat retro (sem coluna).
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { QuarantineTable } from "@/components/normalization/QuarantineTable"
import * as permHooks from "@/hooks/usePermission"
import type { PaginationConfig, QuarantineDetail, QuarantineEntry } from "@/types"

vi.mock("@/hooks/usePermission")

const mockedUsePermission = vi.mocked(permHooks.usePermission)

const FUTURE_DATE = "2099-01-01T00:00:00Z"

const ENTRY_A: QuarantineEntry = {
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

const ENTRY_B: QuarantineEntry = { ...ENTRY_A, id: "q2" }

const PAGINATION: PaginationConfig = {
  current: 1,
  pageSize: 20,
  showTotal: true,
  showSizeChanger: true,
}

const DETAIL: QuarantineDetail = {
  ...ENTRY_A,
  raw_payload: {},
}

function renderTable(
  selectionOverrides: Partial<{
    isSelected: (id: string) => boolean
    headerCheckboxState: "unchecked" | "checked" | "indeterminate"
    toggleOne: (id: string) => void
    toggleAllVisible: () => void
  }> = {},
) {
  const isSelected = selectionOverrides.isSelected ?? vi.fn(() => false)
  const toggleOne = selectionOverrides.toggleOne ?? vi.fn()
  const toggleAllVisible = selectionOverrides.toggleAllVisible ?? vi.fn()
  const headerCheckboxState =
    selectionOverrides.headerCheckboxState ?? "unchecked"

  const utils = render(
    <MemoryRouter>
      <QuarantineTable
        items={[ENTRY_A, ENTRY_B]}
        total={2}
        pagination={PAGINATION}
        onPaginationChange={vi.fn()}
        onDiscard={vi.fn()}
        onReprocess={vi.fn()}
        onGetDetail={vi.fn().mockResolvedValue(DETAIL)}
        onOpenDetail={vi.fn()}
        selection={{
          isSelected,
          toggleOne,
          toggleAllVisible,
          headerCheckboxState,
        }}
      />
    </MemoryRouter>,
  )
  return { ...utils, isSelected, toggleOne, toggleAllVisible }
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUsePermission.mockReturnValue(false)
})

describe("QuarantineTable bulk select (PR #3)", () => {
  it("não renderiza coluna de checkbox quando selection é undefined", () => {
    render(
      <MemoryRouter>
        <QuarantineTable
          items={[ENTRY_A]}
          total={1}
          pagination={PAGINATION}
          onPaginationChange={vi.fn()}
          onDiscard={vi.fn()}
          onReprocess={vi.fn()}
          onGetDetail={vi.fn().mockResolvedValue(DETAIL)}
          onOpenDetail={vi.fn()}
        />
      </MemoryRouter>,
    )
    expect(screen.queryByTestId("quarantine-bulk-header-checkbox")).toBeNull()
    expect(screen.queryByTestId("quarantine-bulk-row-q1")).toBeNull()
  })

  it("renderiza header checkbox + checkbox por linha quando selection é passado", () => {
    renderTable()
    expect(screen.getByTestId("quarantine-bulk-header-checkbox")).toBeInTheDocument()
    expect(screen.getByTestId("quarantine-bulk-row-q1")).toBeInTheDocument()
    expect(screen.getByTestId("quarantine-bulk-row-q2")).toBeInTheDocument()
  })

  it("click em row checkbox chama toggleOne com o id certo", () => {
    const { toggleOne } = renderTable()
    fireEvent.click(screen.getByTestId("quarantine-bulk-row-q1"))
    expect(toggleOne).toHaveBeenCalledTimes(1)
    expect(toggleOne).toHaveBeenCalledWith("q1")
  })

  it("click em header checkbox chama toggleAllVisible", () => {
    const { toggleAllVisible } = renderTable()
    fireEvent.click(screen.getByTestId("quarantine-bulk-header-checkbox"))
    expect(toggleAllVisible).toHaveBeenCalledTimes(1)
  })

  it("header com state=checked aparece marcado", () => {
    renderTable({ headerCheckboxState: "checked" })
    const headerCheckbox = screen.getByTestId(
      "quarantine-bulk-header-checkbox",
    ) as HTMLInputElement
    expect(headerCheckbox.checked).toBe(true)
  })

  it("header com state=indeterminate expõe aria-checked='mixed'", () => {
    renderTable({ headerCheckboxState: "indeterminate" })
    const headerCheckbox = screen.getByTestId(
      "quarantine-bulk-header-checkbox",
    )
    expect(headerCheckbox.getAttribute("aria-checked")).toBe("mixed")
  })

  it("isSelected(id) controla state do checkbox por linha", () => {
    renderTable({ isSelected: (id) => id === "q1" })
    const cb1 = screen.getByTestId("quarantine-bulk-row-q1") as HTMLInputElement
    const cb2 = screen.getByTestId("quarantine-bulk-row-q2") as HTMLInputElement
    expect(cb1.checked).toBe(true)
    expect(cb2.checked).toBe(false)
  })
})
