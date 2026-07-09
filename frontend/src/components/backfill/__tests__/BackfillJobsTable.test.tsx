/**
 * Testes de BackfillJobsTable
 * Cobre: render de cada status, progress bar aria, gating do botão Cancelar.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { BackfillJobsTable } from "@/components/backfill/BackfillJobsTable"
import { usePermission } from "@/hooks/usePermission"
import type { BackfillJob, BackfillJobStatus } from "@/types"
import { vi } from "vitest"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/usePermission")
const mockedUsePermission = vi.mocked(usePermission)

// BackfillJobDetailDrawer usa Modal que usa createPortal — mock simples
vi.mock("@/components/backfill/BackfillJobDetailDrawer", () => ({
  BackfillJobDetailDrawer: () => null,
}))

function makeJob(overrides: Partial<BackfillJob> = {}): BackfillJob {
  return {
    id: "job-abc-123",
    integration_id: 1,
    streams: ["alerts"],
    from_ts: "2026-01-01T00:00:00Z",
    to_ts: "2026-01-10T00:00:00Z",
    status: "completed",
    events_collected: 500,
    events_dispatched: 500,
    progress_pct: 100,
    requested_by_user_id: 1,
    requested_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:01:00Z",
    finished_at: "2026-01-01T00:10:00Z",
    last_error: null,
    cancelled_at: null,
    ...overrides,
  }
}

function renderTable(
  items: BackfillJob[],
  opts?: { canWrite?: boolean; isLoading?: boolean; error?: Error | null },
) {
  const canWrite = opts?.canWrite ?? true
  mockedUsePermission.mockReturnValue(canWrite)

  return render(
    <BackfillJobsTable
      items={items}
      isLoading={opts?.isLoading ?? false}
      error={opts?.error ?? null}
      onCancel={vi.fn().mockResolvedValue(makeJob({ status: "cancelled" }))}
    />,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("BackfillJobsTable", () => {
  it("renderiza data-testid da tabela", () => {
    renderTable([makeJob()])
    expect(screen.getByTestId("backfill-jobs-table")).toBeInTheDocument()
  })

  it("renderiza linha com data-testid correto para o job", () => {
    const job = makeJob({ id: "xyz-9999" })
    renderTable([job])
    expect(screen.getByTestId("backfill-row-xyz-9999")).toBeInTheDocument()
  })

  it("exibe EmptyState quando items está vazio", () => {
    renderTable([])
    expect(screen.getByText(/Nenhum backfill executado ainda/)).toBeInTheDocument()
  })

  it("exibe LoadingSpinner quando isLoading=true", () => {
    renderTable([], { isLoading: true })
    expect(screen.getByText(/Carregando jobs/)).toBeInTheDocument()
  })

  it("exibe Notice de erro quando error está presente", () => {
    renderTable([], { error: new Error("Falha na API") })
    expect(screen.getByText("Falha na API")).toBeInTheDocument()
  })

  const statusCases: [BackfillJobStatus, string][] = [
    ["pending", "Aguardando"],
    ["running", "Em execução"],
    ["completed", "Concluído"],
    ["failed", "Falhou"],
    ["cancelled", "Cancelado"],
  ]

  it.each(statusCases)("status %s exibe label '%s'", (status, label) => {
    renderTable([makeJob({ status })])
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it("progress bar tem aria correto", () => {
    renderTable([makeJob({ progress_pct: 75, status: "running" })])
    const bars = screen.getAllByRole("progressbar")
    const bar = bars[0]
    expect(bar).toHaveAttribute("aria-valuenow", "75")
    expect(bar).toHaveAttribute("aria-valuemin", "0")
    expect(bar).toHaveAttribute("aria-valuemax", "100")
  })

  it("botão Cancelar visível para status pending com permissão", () => {
    const job = makeJob({ id: "cancel-me", status: "pending" })
    renderTable([job], { canWrite: true })
    expect(screen.getByTestId("cancel-backfill-cancel-me")).toBeInTheDocument()
  })

  it("botão Cancelar visível para status running com permissão", () => {
    const job = makeJob({ id: "running-job", status: "running" })
    renderTable([job], { canWrite: true })
    expect(screen.getByTestId("cancel-backfill-running-job")).toBeInTheDocument()
  })

  it("botão Cancelar NÃO visível para status completed", () => {
    const job = makeJob({ id: "done-job", status: "completed" })
    renderTable([job], { canWrite: true })
    expect(screen.queryByTestId("cancel-backfill-done-job")).not.toBeInTheDocument()
  })

  it("botão Cancelar NÃO visível quando sem permissão", () => {
    const job = makeJob({ id: "no-perm-job", status: "pending" })
    renderTable([job], { canWrite: false })
    expect(screen.queryByTestId("cancel-backfill-no-perm-job")).not.toBeInTheDocument()
  })

  it("Detalhes button abre drawer", async () => {
    const job = makeJob({ id: "detail-job" })
    renderTable([job])
    fireEvent.click(screen.getByRole("button", { name: /Detalhes/ }))
    // Drawer está mockado, só verifica que o click não quebra
    await waitFor(() => expect(screen.queryByText(/Carregando jobs/)).not.toBeInTheDocument())
  })
})
