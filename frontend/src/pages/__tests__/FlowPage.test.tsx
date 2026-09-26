/**
 * Testes — FlowPage (poll do grafo + pausa em aba oculta).
 */
import { act, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import FlowPage from "@/pages/FlowPage"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const mockGetFlowGraph = vi.fn()
const mockGetCostSummary = vi.fn()

vi.mock("@/services/api", () => ({
  getFlowGraph: (...args: unknown[]) => mockGetFlowGraph(...args),
  getCostSummary: (...args: unknown[]) => mockGetCostSummary(...args),
}))

const EMPTY_GRAPH = {
  generated_at: "2026-06-19T12:00:00Z",
  window_minutes: 60,
  sources: [],
  routes: [],
  destinations: [],
  totals: { ingest_eps: 0, routed_per_min: 0, drop_per_min: 0, delivered_eps: 0 },
}

function renderPage() {
  return render(
    <MemoryRouter>
      <FlowPage />
    </MemoryRouter>,
  )
}

describe("FlowPage — PERF-09 (poll pausa com a aba oculta)", () => {
  beforeEach(() => {
    mockGetFlowGraph.mockReset()
    mockGetFlowGraph.mockResolvedValue(EMPTY_GRAPH)
    mockGetCostSummary.mockReset()
    mockGetCostSummary.mockResolvedValue({ enabled: false, rows: [] })
  })

  afterEach(() => {
    Object.defineProperty(document, "hidden", { configurable: true, value: false })
  })

  it("carrega o grafo no mount", async () => {
    renderPage()
    await waitFor(() => expect(mockGetFlowGraph).toHaveBeenCalledTimes(1))
    await screen.findByText(/Sem fluxo para exibir/i)
  })

  it("pula o tick do poll (15s) quando a aba está oculta", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderPage()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(mockGetFlowGraph).toHaveBeenCalledTimes(1)

      Object.defineProperty(document, "hidden", { configurable: true, value: true })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15000)
      })
      // Aba oculta: o tick de 15s não deve ter chamado o backend de novo.
      expect(mockGetFlowGraph).toHaveBeenCalledTimes(1)

      Object.defineProperty(document, "hidden", { configurable: true, value: false })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15000)
      })
      // Aba visível de novo: o próximo tick volta a pollar.
      expect(mockGetFlowGraph).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
