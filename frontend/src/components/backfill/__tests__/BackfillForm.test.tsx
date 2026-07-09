/**
 * Testes de BackfillForm
 * Cobre: submit bloqueado (from>=to, janela>90d, sem streams), submit ok, erro do backend.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { BackfillForm } from "@/components/backfill/BackfillForm"
import type { BackfillJob } from "@/types"
import { vi } from "vitest"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const BASE_JOB: BackfillJob = {
  id: "job-001",
  integration_id: 1,
  streams: ["alerts"],
  from_ts: "2026-01-01T00:00:00Z",
  to_ts: "2026-01-10T00:00:00Z",
  status: "pending",
  events_collected: 0,
  events_dispatched: 0,
  progress_pct: 0,
  requested_by_user_id: 1,
  requested_at: new Date().toISOString(),
  started_at: null,
  finished_at: null,
  last_error: null,
  cancelled_at: null,
}

function renderForm(overrides?: {
  onSuccess?: (job: BackfillJob) => void
  onCreateJob?: () => Promise<BackfillJob>
}) {
  const onSuccess = overrides?.onSuccess ?? vi.fn()
  const onCreateJob = overrides?.onCreateJob ?? vi.fn().mockResolvedValue(BASE_JOB)

  render(
    <BackfillForm
      integrationId={1}
      platform="sophos"
      onSuccess={onSuccess}
      onCreateJob={onCreateJob}
    />,
  )

  return { onSuccess, onCreateJob }
}

function setDateInput(testId: string, value: string) {
  const input = screen.getByTestId(testId)
  fireEvent.change(input, { target: { value } })
}

function selectStream(stream: string) {
  const checkbox = screen.getByRole("checkbox", { name: `Stream ${stream}` })
  fireEvent.click(checkbox)
}

describe("BackfillForm", () => {
  it("renderiza o formulário com data-testid correto", () => {
    renderForm()
    expect(screen.getByTestId("backfill-form")).toBeInTheDocument()
    expect(screen.getByTestId("backfill-from-input")).toBeInTheDocument()
    expect(screen.getByTestId("backfill-to-input")).toBeInTheDocument()
    expect(screen.getByTestId("backfill-streams-select")).toBeInTheDocument()
    expect(screen.getByTestId("submit-backfill")).toBeInTheDocument()
  })

  it("submit desabilitado sem intervalo de datas preenchido", () => {
    renderForm()
    const btn = screen.getByTestId("submit-backfill")
    expect(btn).toBeDisabled()
  })

  it("submit desabilitado quando from >= to", () => {
    renderForm()
    setDateInput("backfill-from-input", "2026-01-10T00:00")
    setDateInput("backfill-to-input", "2026-01-01T00:00")
    selectStream("alerts")

    expect(screen.getByTestId("submit-backfill")).toBeDisabled()
    expect(screen.getByText(/data inicial deve ser anterior/)).toBeInTheDocument()
  })

  it("aviso de janela > 90 dias e submit desabilitado", () => {
    renderForm()
    setDateInput("backfill-from-input", "2025-01-01T00:00")
    setDateInput("backfill-to-input", "2025-04-15T00:00") // ~104 dias
    selectStream("alerts")

    expect(screen.getByText(/Janela excede 90 dias/)).toBeInTheDocument()
    expect(screen.getByTestId("submit-backfill")).toBeDisabled()
  })

  it("submit desabilitado sem stream selecionado mesmo com datas válidas", () => {
    renderForm()
    setDateInput("backfill-from-input", "2026-01-01T00:00")
    setDateInput("backfill-to-input", "2026-01-10T00:00")
    // não seleciona nenhum stream

    expect(screen.getByTestId("submit-backfill")).toBeDisabled()
    expect(screen.getByText(/Selecione ao menos um stream/)).toBeInTheDocument()
  })

  it("submit bem-sucedido chama onCreateJob e dispara onSuccess", async () => {
    const onSuccess = vi.fn()
    const onCreateJob = vi.fn().mockResolvedValue(BASE_JOB)
    renderForm({ onSuccess, onCreateJob })

    setDateInput("backfill-from-input", "2026-01-01T00:00")
    setDateInput("backfill-to-input", "2026-01-10T00:00")
    selectStream("alerts")

    const btn = screen.getByTestId("submit-backfill")
    expect(btn).not.toBeDisabled()

    fireEvent.click(btn)

    await waitFor(() => expect(onCreateJob).toHaveBeenCalledTimes(1))
    const callArg = onCreateJob.mock.calls[0][0]
    expect(callArg.streams).toEqual(["alerts"])
    expect(callArg.from_ts).toBeTruthy()
    expect(callArg.to_ts).toBeTruthy()

    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith(BASE_JOB))
  })

  it("notice de erro do backend é exibido quando onCreateJob rejeita", async () => {
    const onCreateJob = vi.fn().mockRejectedValue(new Error("Quota exceeded"))
    renderForm({ onCreateJob })

    setDateInput("backfill-from-input", "2026-01-01T00:00")
    setDateInput("backfill-to-input", "2026-01-10T00:00")
    selectStream("alerts")

    fireEvent.click(screen.getByTestId("submit-backfill"))

    await waitFor(() =>
      expect(screen.getByText("Quota exceeded")).toBeInTheDocument(),
    )
    expect(screen.getByText(/Erro ao criar backfill/)).toBeInTheDocument()
  })

  it("sophos exibe streams alerts, cases e detections", () => {
    renderForm()
    expect(screen.getByRole("checkbox", { name: "Stream alerts" })).toBeInTheDocument()
    expect(screen.getByRole("checkbox", { name: "Stream cases" })).toBeInTheDocument()
    expect(screen.getByRole("checkbox", { name: "Stream detections" })).toBeInTheDocument()
  })

  it("notice informativo está visível", () => {
    renderForm()
    expect(screen.getByText(/fila dedicada/)).toBeInTheDocument()
  })
})
