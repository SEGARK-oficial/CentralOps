/**
 * DetectionsPage tests
 *
 * Cobre:
 * 1. render com Badge de status e severidade corretos.
 * 2. filtro de status chama listDetections com status_filter.
 * 3. Ack chama updateDetectionStatus e reflete no estado local.
 * 4. KPIs calculados por status.
 */

import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { MemoryRouter } from "react-router-dom"
import DetectionsPage from "@/pages/DetectionsPage"
import * as api from "@/services/api"
import type { DetectionRead } from "@/types"

vi.mock("@/services/api")
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: { permissions: ["query.run", "query.save"] },
  }),
}))

const mockedApi = vi.mocked(api)

const DETECTION_OPEN: DetectionRead = {
  id: 1,
  organization_id: 10,
  source: "scheduled_query",
  rule_id: "rule-001",
  rule_name: "Brute Force Detectado",
  severity_id: 4,
  status: "open",
  dedup_key: "org10:rule-001:hash1",
  count: 3,
  first_seen: "2026-06-20T10:00:00Z",
  last_seen: "2026-06-22T08:00:00Z",
  created_at: "2026-06-20T10:00:00Z",
}

const DETECTION_ACK: DetectionRead = {
  id: 2,
  organization_id: 10,
  source: "correlation",
  rule_id: "corr-002",
  rule_name: "Correlação lateral movement",
  severity_id: 5,
  status: "ack",
  dedup_key: "org10:corr-002:hash2",
  count: 1,
  first_seen: "2026-06-21T00:00:00Z",
  last_seen: "2026-06-22T07:00:00Z",
  created_at: "2026-06-21T00:00:00Z",
}

const DETECTION_CLOSED: DetectionRead = {
  id: 3,
  organization_id: 10,
  source: "live_query",
  rule_name: "Acesso indevido",
  severity_id: 2,
  status: "closed",
  dedup_key: "org10:live:hash3",
  count: 1,
  created_at: "2026-06-19T00:00:00Z",
}

function renderPage() {
  return render(
    <MemoryRouter>
      <DetectionsPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listDetections.mockResolvedValue([
    DETECTION_OPEN,
    DETECTION_ACK,
    DETECTION_CLOSED,
  ])
})

describe("DetectionsPage — render base", () => {
  it("exibe Badge de status e severidade para cada detecção", async () => {
    renderPage()

    // Rule names — desktop+mobile renderizam em paralelo no jsdom; getAllByText aceita múltiplos
    expect((await screen.findAllByText("Brute Force Detectado")).length).toBeGreaterThan(0)
    expect(screen.getAllByText("Correlação lateral movement").length).toBeGreaterThan(0)
    expect(screen.getAllByText("Acesso indevido").length).toBeGreaterThan(0)

    // Severity badges (severityId=4 → Alta, 5 → Crítica, 2 → Baixa)
    const altaBadges = screen.getAllByText("Alta")
    expect(altaBadges.length).toBeGreaterThan(0)
    const criticaBadges = screen.getAllByText("Crítica")
    expect(criticaBadges.length).toBeGreaterThan(0)
    const baixaBadges = screen.getAllByText("Baixa")
    expect(baixaBadges.length).toBeGreaterThan(0)

    // Status badges
    const abertaBadges = screen.getAllByText("Aberta")
    expect(abertaBadges.length).toBeGreaterThan(0)
    const reconhecidaBadges = screen.getAllByText("Reconhecida")
    expect(reconhecidaBadges.length).toBeGreaterThan(0)
    const fechadaBadges = screen.getAllByText("Fechada")
    expect(fechadaBadges.length).toBeGreaterThan(0)
  })
})

describe("DetectionsPage — filtro de status", () => {
  it("selecionar 'Abertas' chama listDetections com status_filter=open", async () => {
    renderPage()

    // Wait for initial load — desktop+mobile renderizam em paralelo, findAllByText aceita múltiplos
    await screen.findAllByText("Brute Force Detectado")

    const statusSelect = screen.getByTestId("detections-filter-status")
    fireEvent.click(statusSelect)

    const option = await screen.findByRole("option", { name: "Abertas" })
    fireEvent.click(option)

    await waitFor(() => {
      // Should be called at least twice: initial + after filter
      const calls = mockedApi.listDetections.mock.calls
      const lastCall = calls[calls.length - 1]
      expect(lastCall?.[0]).toMatchObject({ status_filter: "open" })
    })
  })

  it("selecionar 'Todos' chama listDetections sem status_filter", async () => {
    // Start with a filtered state
    mockedApi.listDetections.mockResolvedValue([DETECTION_OPEN])
    renderPage()

    // Wait for initial load — desktop+mobile renderizam em paralelo, findAllByText aceita múltiplos
    await screen.findAllByText("Brute Force Detectado")

    // Apply filter first
    const statusSelect = screen.getByTestId("detections-filter-status")
    fireEvent.click(statusSelect)
    const openOption = await screen.findByRole("option", { name: "Abertas" })
    fireEvent.click(openOption)

    await waitFor(() => {
      const calls = mockedApi.listDetections.mock.calls
      const lastCall = calls[calls.length - 1]
      expect(lastCall?.[0]).toMatchObject({ status_filter: "open" })
    })

    // Reset to "all"
    mockedApi.listDetections.mockResolvedValue([DETECTION_OPEN, DETECTION_ACK, DETECTION_CLOSED])
    fireEvent.click(statusSelect)
    const todosOption = await screen.findByRole("option", { name: "Todos" })
    fireEvent.click(todosOption)

    await waitFor(() => {
      const calls = mockedApi.listDetections.mock.calls
      const lastCall = calls[calls.length - 1]
      // When "Todos" is selected the status_filter key should not be present
      expect(lastCall?.[0]).not.toHaveProperty("status_filter")
    })
  })
})

describe("DetectionsPage — triagem Ack", () => {
  it("clicar em linha abre drawer; clicar em Ack chama updateDetectionStatus e reflete no estado", async () => {
    const updatedDetection: DetectionRead = { ...DETECTION_OPEN, status: "ack" }
    mockedApi.updateDetectionStatus.mockResolvedValue(updatedDetection)

    renderPage()

    // Click the row for DETECTION_OPEN (desktop table tr + mobile button both render in jsdom)
    const rows = await screen.findAllByRole("button", {
      name: /Ver detalhes da detecção Brute Force Detectado/i,
    })
    fireEvent.click(rows[0])

    // Drawer should open
    expect(await screen.findByRole("dialog", { name: /Detalhes da detecção/i })).toBeInTheDocument()

    // Ack button should be visible (status is open)
    const ackBtn = screen.getByRole("button", { name: /Reconhecer \(Ack\)/i })
    fireEvent.click(ackBtn)

    await waitFor(() => {
      expect(mockedApi.updateDetectionStatus).toHaveBeenCalledWith(
        DETECTION_OPEN.id,
        { status: "ack" },
      )
    })
  })
})

describe("DetectionsPage — KPIs", () => {
  it("exibe contagens corretas por status", async () => {
    renderPage()

    // Wait for load — desktop+mobile renderizam em paralelo, findAllByText aceita múltiplos
    await screen.findAllByText("Brute Force Detectado")

    expect(screen.getByTestId("kpi-total").textContent).toBe("3")
    expect(screen.getByTestId("kpi-open").textContent).toBe("1")
    expect(screen.getByTestId("kpi-ack").textContent).toBe("1")
    expect(screen.getByTestId("kpi-closed").textContent).toBe("1")
  })

  it("KPIs refletem lista vazia quando não há detecções", async () => {
    mockedApi.listDetections.mockResolvedValue([])
    renderPage()

    await waitFor(() => {
      expect(screen.getByTestId("kpi-total").textContent).toBe("0")
    })
    expect(screen.getByTestId("kpi-open").textContent).toBe("0")
    expect(screen.getByTestId("kpi-ack").textContent).toBe("0")
    expect(screen.getByTestId("kpi-closed").textContent).toBe("0")
  })
})
