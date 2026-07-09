/**
 * Testes de IntegrationBackfillPanel
 * Cobre: aba renderiza, gating "Novo backfill", abre modal.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { IntegrationBackfillPanel } from "@/components/backfill/IntegrationBackfillPanel"
import { usePermission } from "@/hooks/usePermission"
import { useBackfillJobs } from "@/hooks/useBackfillJobs"
import type { BackfillJob } from "@/types"
import { vi } from "vitest"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/usePermission")
vi.mock("@/hooks/useBackfillJobs")

// BackfillJobsTable também usa usePermission internamente — mock já feito
// BackfillForm é renderizado dentro do Modal; mock simples
vi.mock("@/components/backfill/BackfillForm", () => ({
  BackfillForm: ({ onCancel }: { onCancel?: () => void }) => (
    <div data-testid="backfill-form-mock">
      <button type="button" onClick={onCancel}>
        Cancelar (mock)
      </button>
    </div>
  ),
}))

// BackfillJobsTable mock mínimo
vi.mock("@/components/backfill/BackfillJobsTable", () => ({
  BackfillJobsTable: ({ items }: { items: BackfillJob[] }) => (
    <div data-testid="backfill-jobs-table-mock">
      {items.length === 0 ? "Nenhum job" : `${items.length} job(s)`}
    </div>
  ),
}))

const mockedUsePermission = vi.mocked(usePermission)
const mockedUseBackfillJobs = vi.mocked(useBackfillJobs)

function mockHook(items: BackfillJob[] = []) {
  mockedUseBackfillJobs.mockReturnValue({
    items,
    total: items.length,
    isLoading: false,
    error: null,
    refetch: vi.fn(),
    createJob: vi.fn().mockResolvedValue({} as BackfillJob),
    cancelJob: vi.fn().mockResolvedValue({} as BackfillJob),
  })
}

function renderPanel(canWrite = true) {
  mockedUsePermission.mockReturnValue(canWrite)
  mockHook()
  return render(<IntegrationBackfillPanel integrationId={1} platform="sophos" />)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("IntegrationBackfillPanel", () => {
  it("renderiza data-testid do painel", () => {
    renderPanel()
    expect(screen.getByTestId("backfill-panel")).toBeInTheDocument()
  })

  it("título 'Backfill — coleta histórica controlada' está presente", () => {
    renderPanel()
    expect(screen.getByText(/Backfill — coleta histórica controlada/)).toBeInTheDocument()
  })

  it("botão 'Novo backfill' visível com permissão integration.write", () => {
    renderPanel(true)
    expect(screen.getByTestId("new-backfill-button")).toBeInTheDocument()
  })

  it("botão 'Novo backfill' NÃO visível sem permissão", () => {
    renderPanel(false)
    expect(screen.queryByTestId("new-backfill-button")).not.toBeInTheDocument()
  })

  it("clicar em 'Novo backfill' abre modal com BackfillForm", async () => {
    renderPanel(true)
    fireEvent.click(screen.getByTestId("new-backfill-button"))
    await waitFor(() =>
      expect(screen.getByTestId("backfill-form-mock")).toBeInTheDocument(),
    )
  })

  it("fechar modal (onCancel) remove o form da tela", async () => {
    renderPanel(true)
    fireEvent.click(screen.getByTestId("new-backfill-button"))

    await waitFor(() =>
      expect(screen.getByTestId("backfill-form-mock")).toBeInTheDocument(),
    )

    // Clica no botão Cancelar dentro do mock do form
    fireEvent.click(screen.getByText("Cancelar (mock)"))

    await waitFor(() =>
      expect(screen.queryByTestId("backfill-form-mock")).not.toBeInTheDocument(),
    )
  })

  it("renderiza BackfillJobsTable com a lista de jobs", () => {
    renderPanel()
    expect(screen.getByTestId("backfill-jobs-table-mock")).toBeInTheDocument()
    expect(screen.getByText("Nenhum job")).toBeInTheDocument()
  })
})
