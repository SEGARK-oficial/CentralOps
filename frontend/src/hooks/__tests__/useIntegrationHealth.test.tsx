/**
 * Testes de useIntegrationHealth
 * Cobre: GET sucesso, auto-refresh dispara após interval, pausa quando document.hidden,
 *        refetch manual, abort no unmount.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useIntegrationHealth } from "@/hooks/useIntegrationHealth"
import * as api from "@/services/api"
import type { IntegrationPipelineHealth } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    getIntegrationPipelineHealth: vi.fn(),
  }
})

const mockedApi = vi.mocked(api)

const HEALTH_DATA: IntegrationPipelineHealth = {
  integration_id: 1,
  status: "healthy",
  events_per_minute: 42,
  lag_seconds: 5,
  last_error: null,
  last_success_at: "2026-04-25T10:00:00Z",
  mapped_field_ratio: 0.87,
  drift_count_24h: 3,
  quarantine_count_24h: 1,
  cached_at: new Date().toISOString(),
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => "visible",
  })
})

// ── Testes com timers reais ───────────────────────────────────────────────────

describe("useIntegrationHealth — fetch básico", () => {
  it("retorna data após GET bem-sucedido", async () => {
    mockedApi.getIntegrationPipelineHealth.mockResolvedValue(HEALTH_DATA)

    const { result } = renderHook(() => useIntegrationHealth(1))

    expect(result.current.isLoading).toBe(true)
    expect(result.current.data).toBeNull()

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.data).toEqual(HEALTH_DATA)
    expect(result.current.error).toBeNull()
  })

  it("popula error em caso de falha de rede", async () => {
    mockedApi.getIntegrationPipelineHealth.mockRejectedValue(new Error("Network error"))

    const { result } = renderHook(() => useIntegrationHealth(1))

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error?.message).toBe("Network error")
    expect(result.current.data).toBeNull()
  })

  it("refetch manual re-executa a busca", async () => {
    mockedApi.getIntegrationPipelineHealth.mockResolvedValue(HEALTH_DATA)

    const { result } = renderHook(() => useIntegrationHealth(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(1)

    act(() => result.current.refetch())
    await waitFor(() => expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(2))
  })

  it("cancela a requisição no unmount sem gerar erro", async () => {
    let aborted = false
    mockedApi.getIntegrationPipelineHealth.mockImplementation((_id, opts) =>
      new Promise((_res, rej) => {
        opts?.signal?.addEventListener("abort", () => {
          aborted = true
          rej(new DOMException("Aborted", "AbortError"))
        })
      }),
    )

    const { result, unmount } = renderHook(() => useIntegrationHealth(1))
    unmount()

    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })

    expect(aborted).toBe(true)
    expect(result.current.error).toBeNull()
  })
})

// ── Testes com fake timers (auto-refresh e visibilidade) ─────────────────────

describe("useIntegrationHealth — polling", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it("auto-refresh dispara após interval padrão (60s)", async () => {
    mockedApi.getIntegrationPipelineHealth.mockResolvedValue(HEALTH_DATA)

    const { result } = renderHook(() => useIntegrationHealth(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(1)

    act(() => vi.advanceTimersByTime(60_000))

    await waitFor(() => expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(2))
  })

  it("não auto-refresha quando página está oculta (document.hidden)", async () => {
    mockedApi.getIntegrationPipelineHealth.mockResolvedValue(HEALTH_DATA)

    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    })

    const { result } = renderHook(() => useIntegrationHealth(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(1)

    act(() => vi.advanceTimersByTime(60_000))

    // Continua sendo 1 — polling pausado
    expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(1)
  })

  it("auto-refresh com intervalo customizado (10s)", async () => {
    mockedApi.getIntegrationPipelineHealth.mockResolvedValue(HEALTH_DATA)

    const { result } = renderHook(() => useIntegrationHealth(1, { refreshIntervalMs: 10_000 }))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    act(() => vi.advanceTimersByTime(10_000))

    await waitFor(() => expect(mockedApi.getIntegrationPipelineHealth).toHaveBeenCalledTimes(2))
  })
})
