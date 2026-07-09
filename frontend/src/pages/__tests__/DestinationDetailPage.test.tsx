/**
 * DestinationDetailPage tests (C7 — UX de destinos).
 *
 * Cobre:
 * - Aba Saúde: renderiza eps e bytes_per_min do DestinationHealth
 * - Aba Saúde: Sparkline com séries (enviados/rejeitados)
 * - DLQ: botão "Reprocessar tudo" abre ConfirmDialog → chama reprocessDestinationDlq
 * - DLQ: botão de reprocessar entrada individual
 * - DLQ: aria-expanded no botão de expandir entrada
 * - Credencial (aba): rotateCredential e revokeCredential chamam o client
 * - Estado loading e erro na carga inicial
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import DestinationDetailPage from "@/pages/DestinationDetailPage"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type {
  Destination,
  DestinationHealth,
  DestinationDlqResponse,
  DestinationMetrics,
  CredentialAuditResponse,
} from "@/types"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")

const mockedApi = vi.mocked(api)

// ── Fixtures ──────────────────────────────────────────────────────────────────

const DEST: Destination = {
  id: "dest-001",
  name: "Splunk HEC Prod",
  kind: "splunk_hec",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: null,
  created_at: "2024-01-01T00:00:00Z",
  updated_at: "2024-01-01T00:00:00Z",
  has_secret: true,
}

const HEALTH: DestinationHealth = {
  destination_id: "dest-001",
  status: "healthy",
  enabled: true,
  breaker_state: "closed",
  dlq_total: 5,
  dlq_24h: 2,
  last_dlq_at: "2024-06-01T10:00:00Z",
  eps: 12.5,
  bytes_per_min: 2048000,
}

const METRICS: DestinationMetrics = {
  destination_id: "dest-001",
  available: true,
  reason: null,
  series: {
    sent: [[1700000000000, 100], [1700000060000, 120]],
    rejected: [[1700000000000, 3], [1700000060000, 1]],
    latency_avg: [[1700000000000, 0.05], [1700000060000, 0.04]],
  },
  gauges: { queue_depth: null, backpressure_state: null },
  dlq_total: 5,
  dlq_24h: 2,
  by_error_kind: {},
  breaker_state: "closed",
}

const DLQ: DestinationDlqResponse = {
  destination_id: "dest-001",
  total: 2,
  by_error_kind: { timeout: 1, auth_failure: 1 },
  entries: [
    {
      id: "dlq-entry-001",
      event_id: "evt-abc-001",
      error_kind: "timeout",
      error_detail: "Conexão expirou",
      payload: { foo: "bar" },
      organization_id: null,
      created_at: "2024-06-01T10:00:00Z",
    },
    {
      id: "dlq-entry-002",
      event_id: "evt-abc-002",
      error_kind: "auth_failure",
      error_detail: null,
      payload: null,
      organization_id: null,
      created_at: "2024-06-01T11:00:00Z",
    },
  ],
}

const AUDIT: CredentialAuditResponse = {
  destination_id: "dest-001",
  total: 2,
  entries: [
    {
      id: "audit-001",
      destination_id: "dest-001",
      actor: "admin@centralops.io",
      action: "rotate",
      organization_id: null,
      detail: null,
      created_at: "2024-06-01T09:00:00Z",
    },
    {
      id: "audit-002",
      destination_id: "dest-001",
      actor: "system",
      action: "test",
      organization_id: null,
      detail: null,
      created_at: "2024-06-01T08:00:00Z",
    },
  ],
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function renderPage(destId = "dest-001") {
  return render(
    <MemoryRouter initialEntries={[`/destinations/${destId}`]}>
      <Routes>
        <Route path="/destinations/:id" element={<DestinationDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

function setupDefaultMocks() {
  mockedApi.getDestination.mockResolvedValue(DEST)
  mockedApi.getDestinationHealth.mockResolvedValue(HEALTH)
  mockedApi.getDestinationMetrics.mockResolvedValue(METRICS)
  mockedApi.getDestinationDlq.mockResolvedValue(DLQ)
  mockedApi.getDestinationTap.mockResolvedValue({ destination_id: "dest-001", entries: [] })
  mockedApi.getCredentialAudit.mockResolvedValue(AUDIT)
  // DestinationForm (aba Configuração) chama listDestinationTypes ao montar
  mockedApi.listDestinationTypes.mockResolvedValue([])
}

// ── Testes ────────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks()
  setupDefaultMocks()
})

describe("DestinationDetailPage — loading + erro", () => {
  it("mostra card de carregamento antes de resolver", () => {
    mockedApi.getDestination.mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByText(/Carregando…/i)).toBeInTheDocument()
  })

  it("mostra Notice de erro se getDestination rejeitar", async () => {
    mockedApi.getDestination.mockRejectedValue(new Error("Not found"))
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Destino indisponível/i)).toBeInTheDocument()
    })
  })
})

describe("DestinationDetailPage — aba Saúde: eps e bytes_per_min", () => {
  it("renderiza eps na aba Saúde", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    // Navegar para aba Saúde
    fireEvent.click(screen.getByRole("tab", { name: /Saúde/i }))

    await waitFor(() => {
      expect(screen.getByText("Eventos/s (eps)")).toBeInTheDocument()
      expect(screen.getByText("12.50")).toBeInTheDocument()
    })
  })

  it("renderiza bytes_per_min formatado humanamente", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Saúde/i }))

    await waitFor(() => {
      expect(screen.getByText("Volume/min")).toBeInTheDocument()
      // 2048000 bytes = 1953.1 KB/min → ~1953.1 KB/min
      expect(screen.getByText(/KB\/min|MB\/min/)).toBeInTheDocument()
    })
  })

  it("renderiza Sparkline de enviados e rejeitados com variantes corretas", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Saúde/i }))

    await waitFor(() => {
      // Sparkline renderiza labels como texto
      expect(screen.getByText("eventos/min")).toBeInTheDocument()
      expect(screen.getByText("rejeitados/min")).toBeInTheDocument()
    })
  })
})

describe("DestinationDetailPage — DLQ reprocess", () => {
  it("botão 'Reprocessar tudo' está visível quando há entradas na DLQ", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /DLQ/i }))

    await waitFor(() => {
      expect(screen.getByTestId("btn-reprocess-all")).toBeInTheDocument()
    })
  })

  it("confirmar reprocessamento em lote chama reprocessDestinationDlq sem eventIds", async () => {
    mockedApi.reprocessDestinationDlq.mockResolvedValue({
      destination_id: "dest-001",
      task_id: "task-xyz",
      queued: 2,
    })

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /DLQ/i }))
    await waitFor(() => expect(screen.getByTestId("btn-reprocess-all")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("btn-reprocess-all"))

    // Dialog abre
    await waitFor(() => {
      expect(screen.getByText("Reprocessar DLQ completa")).toBeInTheDocument()
    })

    // Confirmar
    fireEvent.click(screen.getByTestId("dlq-reprocess-all-dialog-confirm"))

    await waitFor(() => {
      expect(mockedApi.reprocessDestinationDlq).toHaveBeenCalledWith("dest-001", undefined)
    })
  })

  it("reprocessar entrada individual chama reprocessDestinationDlq com event_id", async () => {
    mockedApi.reprocessDestinationDlq.mockResolvedValue({
      destination_id: "dest-001",
      task_id: "task-abc",
      queued: 1,
    })

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /DLQ/i }))
    await waitFor(() => expect(screen.getByTestId("btn-reprocess-entry-evt-abc-001")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("btn-reprocess-entry-evt-abc-001"))

    await waitFor(() => {
      expect(screen.getByText("Reprocessar evento")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId("dlq-reprocess-entry-dialog-confirm"))

    await waitFor(() => {
      expect(mockedApi.reprocessDestinationDlq).toHaveBeenCalledWith("dest-001", ["evt-abc-001"])
    })
  })

  it("toast de sucesso exibe queued e task_id após reprocess", async () => {
    mockedApi.reprocessDestinationDlq.mockResolvedValue({
      destination_id: "dest-001",
      task_id: "task-xyz-999",
      queued: 2,
    })

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /DLQ/i }))
    await waitFor(() => expect(screen.getByTestId("btn-reprocess-all")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("btn-reprocess-all"))
    await waitFor(() => expect(screen.getByText("Reprocessar DLQ completa")).toBeInTheDocument())
    fireEvent.click(screen.getByTestId("dlq-reprocess-all-dialog-confirm"))

    await waitFor(() => {
      expect(screen.getByText(/Reprocessamento enfileirado/i)).toBeInTheDocument()
      expect(screen.getByText(/task-xyz-999/)).toBeInTheDocument()
    })
  })

  it("botão de expandir entrada DLQ tem aria-expanded", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /DLQ/i }))

    await waitFor(() => {
      expect(screen.getByText("evt-abc-001")).toBeInTheDocument()
    })

    // Botão de expandir é identificado pelo aria-controls (cada entrada tem id único)
    const expandBtn = document.querySelector<HTMLButtonElement>("[aria-controls='dlq-payload-dlq-entry-001']")
    expect(expandBtn).not.toBeNull()
    expect(expandBtn).toHaveAttribute("aria-expanded", "false")

    // Clicar expande
    fireEvent.click(expandBtn!)
    expect(expandBtn).toHaveAttribute("aria-expanded", "true")
  })
})

describe("DestinationDetailPage — aba Credencial", () => {
  it("aba Credencial é visível quando has_secret=true", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    expect(screen.getByRole("tab", { name: /Credencial/i })).toBeInTheDocument()
  })

  it("aba Credencial ausente quando has_secret=false", async () => {
    const destSemSecret: Destination = { ...DEST, has_secret: false }
    mockedApi.getDestination.mockResolvedValue(destSemSecret)

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    expect(screen.queryByRole("tab", { name: /Credencial/i })).not.toBeInTheDocument()
  })

  it("clicar em Rotacionar abre o modal de rotação", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Credencial/i }))

    await waitFor(() => {
      expect(screen.getByTestId("btn-rotate-credential")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId("btn-rotate-credential"))

    await waitFor(() => {
      // O input de segredo fica visível somente dentro do modal
      expect(screen.getByTestId("rotate-secret-input")).toBeInTheDocument()
    })
  })

  it("rotateCredential é chamado ao submeter o formulário de rotação", async () => {
    mockedApi.rotateCredential.mockResolvedValue({
      destination_id: "dest-001",
      secret_version: 2,
      secret_rotated_at: "2024-06-17T00:00:00Z",
      secret_expires_at: null,
      has_secret: true,
    })

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Credencial/i }))
    await waitFor(() => expect(screen.getByTestId("btn-rotate-credential")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("btn-rotate-credential"))
    await waitFor(() => expect(screen.getByTestId("rotate-secret-input")).toBeInTheDocument())

    fireEvent.change(screen.getByTestId("rotate-secret-input"), {
      target: { value: "novo-segredo-seguro-123" },
    })

    fireEvent.click(screen.getByTestId("rotate-submit-btn"))

    await waitFor(() => {
      expect(mockedApi.rotateCredential).toHaveBeenCalledWith("dest-001", {
        new_secret: "novo-segredo-seguro-123",
        expires_at: null,
      })
    })
  })

  it("revokeCredential é chamado ao confirmar revogação", async () => {
    mockedApi.revokeCredential.mockResolvedValue({
      destination_id: "dest-001",
      enabled: false,
      secret_revoked_at: "2024-06-17T00:00:00Z",
      has_secret: false,
    })

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Credencial/i }))
    await waitFor(() => expect(screen.getByTestId("btn-revoke-credential")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("btn-revoke-credential"))

    await waitFor(() => {
      expect(screen.getByText("Revogar credencial")).toBeInTheDocument()
    })

    fireEvent.click(screen.getByTestId("revoke-confirm-dialog-confirm"))

    await waitFor(() => {
      expect(mockedApi.revokeCredential).toHaveBeenCalledWith("dest-001")
    })
  })

  it("lista entradas de auditoria na aba Credencial", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Credencial/i }))

    await waitFor(() => {
      expect(screen.getByText("admin@centralops.io")).toBeInTheDocument()
      expect(screen.getByText("Auditoria de acesso")).toBeInTheDocument()
    })
  })
})

describe("DestinationDetailPage — Lineage", () => {
  it("campo de busca de lineage está presente na aba Saúde", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Saúde/i }))

    await waitFor(() => {
      expect(screen.getByTestId("lineage-event-id-input")).toBeInTheDocument()
    })
  })

  it("buscar lineage chama getDestinationLineage com o event_id digitado", async () => {
    mockedApi.getDestinationLineage.mockResolvedValue({
      destination_id: "dest-001",
      event_id: "evt-test-123",
      entries: [
        {
          destination_id: "dest-001",
          kind: "splunk_hec",
          status: "delivered",
          ts: 1700000000,
        },
      ],
      retention_note: "Dados retidos por 7 dias (Redis TTL).",
    })

    renderPage()
    await waitFor(() => expect(screen.getByText("Splunk HEC Prod")).toBeInTheDocument())

    fireEvent.click(screen.getByRole("tab", { name: /Saúde/i }))

    await waitFor(() => expect(screen.getByTestId("lineage-event-id-input")).toBeInTheDocument())

    fireEvent.change(screen.getByTestId("lineage-event-id-input"), {
      target: { value: "evt-test-123" },
    })

    fireEvent.click(screen.getByTestId("lineage-search-btn"))

    await waitFor(() => {
      expect(mockedApi.getDestinationLineage).toHaveBeenCalledWith("dest-001", "evt-test-123")
    })

    await waitFor(() => {
      expect(screen.getByTestId("lineage-result")).toBeInTheDocument()
      expect(screen.getByText("delivered")).toBeInTheDocument()
      expect(screen.getByText(/7 dias/i)).toBeInTheDocument()
    })
  })
})
