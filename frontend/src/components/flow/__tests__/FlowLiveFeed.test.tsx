/**
 * Testes — FlowLiveFeed (feed ao vivo colapsável).
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react"
import { FlowLiveFeed } from "@/components/flow/FlowLiveFeed"
import type { TopologyDestination } from "@/types"
import i18n from "@/i18n"

// Sem o bootstrap do i18n o i18next nunca é inicializado neste processo e toda
// asserção de texto bate contra a chave crua. `changeLanguage("pt")` fixa o
// catálogo contra o qual as asserções abaixo foram escritas (o detector
// resolveria o navigator do jsdom, "en-US").
beforeAll(async () => {
  await i18n.changeLanguage("pt")
})


const DESTS: TopologyDestination[] = [
  { id: "d1", name: "Splunk Prod", kind: "splunk_hec", status: "healthy", eps: 133 },
  { id: "d2", name: "S3 Archive", kind: "s3", status: "healthy", eps: 52 },
]

const mockGetDestinationTap = vi.fn()

vi.mock("@/services/api", () => ({
  get getDestinationTap() {
    return mockGetDestinationTap
  },
}))

const TAP_RESPONSE = {
  destination_id: "d1",
  entries: [
    { timestamp: "2026-06-19T12:00:00Z", event_type: "alert", _redacted: false },
    { timestamp: "2026-06-19T11:59:00Z", event_type: "login", _redacted: true },
  ],
}

describe("FlowLiveFeed", () => {
  beforeEach(() => {
    mockGetDestinationTap.mockReset()
    mockGetDestinationTap.mockResolvedValue(TAP_RESPONSE)
  })

  it("renderiza botão de toggle", () => {
    render(<FlowLiveFeed destinations={DESTS} open={false} onToggle={vi.fn()} />)
    expect(screen.getByRole("button", { name: /Feed ao vivo/i })).toBeInTheDocument()
  })

  it("colapsado: não mostra lista de eventos", () => {
    render(<FlowLiveFeed destinations={DESTS} open={false} onToggle={vi.fn()} />)
    expect(screen.queryByTestId("flow-live-feed-body")).not.toBeInTheDocument()
  })

  it("aberto: mostra corpo do feed", () => {
    render(<FlowLiveFeed destinations={DESTS} open={true} onToggle={vi.fn()} />)
    expect(screen.getByTestId("flow-live-feed-body")).toBeInTheDocument()
  })

  it("aberto: carrega e exibe eventos dos destinos", async () => {
    render(<FlowLiveFeed destinations={DESTS} open={true} onToggle={vi.fn()} />)
    await waitFor(
      () => {
        // "alert" e "login" devem aparecer após o fetch
        const items = screen.getAllByText(/alert|login/)
        expect(items.length).toBeGreaterThan(0)
      },
      { timeout: 3000 },
    )
  })

  it("toggle chama onToggle ao clicar", () => {
    const onToggle = vi.fn()
    render(<FlowLiveFeed destinations={DESTS} open={false} onToggle={onToggle} />)
    const btn = screen.getByRole("button", { name: /Feed ao vivo/i })
    fireEvent.click(btn)
    expect(onToggle).toHaveBeenCalledOnce()
  })

  it("aria-expanded correto quando fechado", () => {
    render(<FlowLiveFeed destinations={DESTS} open={false} onToggle={vi.fn()} />)
    const btn = screen.getByRole("button", { name: /Feed ao vivo/i })
    expect(btn.getAttribute("aria-expanded")).toBe("false")
  })

  it("aria-expanded correto quando aberto", () => {
    render(<FlowLiveFeed destinations={DESTS} open={true} onToggle={vi.fn()} />)
    const btn = screen.getByRole("button", { name: /Feed ao vivo/i })
    expect(btn.getAttribute("aria-expanded")).toBe("true")
  })

  it("sem destinos: mostra mensagem adequada quando aberto", async () => {
    render(<FlowLiveFeed destinations={[]} open={true} onToggle={vi.fn()} />)
    // Com destinos=[], fetchFeed retorna cedo; lista fica vazia
    await waitFor(
      () => {
        expect(screen.getByText(/Nenhum evento recente/i)).toBeInTheDocument()
      },
      { timeout: 3000 },
    )
  })

  it("PERF-09: pula o poll com a aba oculta (document.hidden)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      render(<FlowLiveFeed destinations={DESTS} open={true} onToggle={vi.fn()} />)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      const callsAfterMount = mockGetDestinationTap.mock.calls.length
      expect(callsAfterMount).toBeGreaterThan(0)

      Object.defineProperty(document, "hidden", { configurable: true, value: true })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(8000)
      })
      // Aba oculta: o tick do poll (8s) não deve ter disparado nenhum fetch novo.
      expect(mockGetDestinationTap.mock.calls.length).toBe(callsAfterMount)

      Object.defineProperty(document, "hidden", { configurable: true, value: false })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(8000)
      })
      // Aba visível de novo: o próximo tick volta a pollar.
      expect(mockGetDestinationTap.mock.calls.length).toBeGreaterThan(callsAfterMount)
    } finally {
      Object.defineProperty(document, "hidden", { configurable: true, value: false })
      vi.useRealTimers()
    }
  })

  it("PERF-16: desmontar durante os 2s do destaque 'isNew' não lança (timeout limpo no unmount)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      const { unmount } = render(<FlowLiveFeed destinations={DESTS} open={true} onToggle={vi.fn()} />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      // Desmonta ANTES dos 2s do setTimeout que limpa `isNew` — sem o cleanup,
      // esse timeout dispararia depois, tentando setState em componente morto.
      expect(() => unmount()).not.toThrow()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it("degrada gracioso se um tap falhar (não crasha)", async () => {
    // primeiro destino ok, segundo falha
    mockGetDestinationTap
      .mockResolvedValueOnce({ destination_id: "d1", entries: [] })
      .mockRejectedValueOnce(new Error("Network error"))

    await act(async () => {
      render(<FlowLiveFeed destinations={DESTS} open={true} onToggle={vi.fn()} />)
    })
    // Não deve ter crash — feed body ainda está na DOM
    expect(screen.getByTestId("flow-live-feed-body")).toBeInTheDocument()
  })
})
