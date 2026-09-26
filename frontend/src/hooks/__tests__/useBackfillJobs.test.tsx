/**
 * Testes de useBackfillJobs
 * Cobre: list, create, cancel, polling, refetch, erro de rede.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useBackfillJobs } from "@/hooks/useBackfillJobs"
import * as api from "@/services/api"
import type { BackfillJob } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listBackfillJobs: vi.fn(),
    createBackfillJob: vi.fn(),
    cancelBackfillJob: vi.fn(),
  }
})

const mockedApi = vi.mocked(api)

const JOB_1: BackfillJob = {
  id: "aaaa-1111",
  integration_id: 1,
  streams: ["alerts"],
  from_ts: "2026-01-01T00:00:00Z",
  to_ts: "2026-01-10T00:00:00Z",
  status: "completed",
  events_collected: 100,
  events_dispatched: 100,
  progress_pct: 100,
  requested_by_user_id: 1,
  requested_at: "2026-01-01T00:00:00Z",
  started_at: "2026-01-01T00:01:00Z",
  finished_at: "2026-01-01T00:10:00Z",
  last_error: null,
  cancelled_at: null,
}

const LIST_RESPONSE = { items: [JOB_1], total: 1, limit: 50, offset: 0 }

beforeEach(() => {
  vi.clearAllMocks()
})

// ── Testes com timers reais ───────────────────────────────────────────────────

describe("useBackfillJobs — state", () => {
  it("retorna items após list bem-sucedido", async () => {
    mockedApi.listBackfillJobs.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() => useBackfillJobs(1))

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.items).toEqual([JOB_1])
    expect(result.current.total).toBe(1)
    expect(result.current.error).toBeNull()
  })

  it("popula error em falha de rede", async () => {
    mockedApi.listBackfillJobs.mockRejectedValue(new Error("Network error"))

    const { result } = renderHook(() => useBackfillJobs(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error?.message).toBe("Network error")
    expect(result.current.items).toEqual([])
  })

  it("refetch re-executa listBackfillJobs", async () => {
    mockedApi.listBackfillJobs.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() => useBackfillJobs(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(mockedApi.listBackfillJobs).toHaveBeenCalledTimes(1)

    act(() => result.current.refetch())
    await waitFor(() => expect(mockedApi.listBackfillJobs).toHaveBeenCalledTimes(2))
  })

  it("createJob chama API e dispara refetch", async () => {
    const newJob: BackfillJob = { ...JOB_1, id: "bbbb-2222", status: "pending" }
    mockedApi.listBackfillJobs.mockResolvedValue(LIST_RESPONSE)
    mockedApi.createBackfillJob.mockResolvedValue(newJob)

    const { result } = renderHook(() => useBackfillJobs(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    const callCountBefore = mockedApi.listBackfillJobs.mock.calls.length

    let createdJob: BackfillJob | undefined
    await act(async () => {
      createdJob = await result.current.createJob({
        streams: ["alerts"],
        from_ts: "2026-01-01T00:00:00Z",
        to_ts: "2026-01-10T00:00:00Z",
      })
    })

    expect(createdJob).toEqual(newJob)
    await waitFor(() =>
      expect(mockedApi.listBackfillJobs.mock.calls.length).toBeGreaterThan(callCountBefore),
    )
  })

  it("cancelJob chama API e dispara refetch", async () => {
    const cancelledJob: BackfillJob = { ...JOB_1, status: "cancelled" }
    mockedApi.listBackfillJobs.mockResolvedValue(LIST_RESPONSE)
    mockedApi.cancelBackfillJob.mockResolvedValue(cancelledJob)

    const { result } = renderHook(() => useBackfillJobs(1))
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    const callCountBefore = mockedApi.listBackfillJobs.mock.calls.length

    await act(async () => {
      await result.current.cancelJob("aaaa-1111")
    })

    expect(mockedApi.cancelBackfillJob).toHaveBeenCalledWith("aaaa-1111")
    await waitFor(() =>
      expect(mockedApi.listBackfillJobs.mock.calls.length).toBeGreaterThan(callCountBefore),
    )
  })
})

// ── Testes de polling com fake timers ─────────────────────────────────────────

describe("useBackfillJobs — polling", () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  async function flush(ms: number) {
    await act(async () => { vi.advanceTimersByTime(ms) })
  }

  it("polling dispara refetch após o intervalo", async () => {
    mockedApi.listBackfillJobs.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() =>
      useBackfillJobs(1, undefined, { refreshIntervalMs: 5000 }),
    )

    // Drena a promise inicial
    await act(async () => { await Promise.resolve() })
    const callsBefore = mockedApi.listBackfillJobs.mock.calls.length

    await flush(5001)
    // Drena a promise do refetch
    await act(async () => { await Promise.resolve() })

    expect(mockedApi.listBackfillJobs.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(result.current).toBeDefined()
  })

  // PERF-07: o tick de poll não pode ligar `isLoading` — antes, isto trocava
  // a tabela inteira por um spinner a cada 10s, perdendo scroll/foco.
  it("o tick de poll NÃO liga isLoading (silencioso)", async () => {
    mockedApi.listBackfillJobs.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() =>
      useBackfillJobs(1, undefined, { refreshIntervalMs: 5000 }),
    )
    await act(async () => { await Promise.resolve() })
    expect(result.current.isLoading).toBe(false)

    const isLoadingDuringPoll: boolean[] = []
    // Uma 2ª resposta "lenta" (nunca resolvida no teste) deixaria isLoading
    // preso em `true` se o poll o ligasse — checamos logo após o tick.
    await flush(5001)
    isLoadingDuringPoll.push(result.current.isLoading)
    await act(async () => { await Promise.resolve() })

    expect(isLoadingDuringPoll.every((v) => v === false)).toBe(true)
    expect(result.current.isLoading).toBe(false)
  })
})

// ── R2-5.4: abort da requisição anterior ──────────────────────────────────────

describe("useBackfillJobs — aborta o fetch anterior ainda em voo", () => {
  it("um refetch novo aborta o controller do fetch anterior — a resposta velha (fora de ordem) não vence", async () => {
    interface Call {
      signal?: AbortSignal | null
      resolve: (v: typeof LIST_RESPONSE) => void
    }
    const calls: Call[] = []
    mockedApi.listBackfillJobs.mockImplementation(
      (_id: number, _filters?: unknown, opts?: { signal?: AbortSignal | null }) =>
        new Promise<typeof LIST_RESPONSE>((resolve, reject) => {
          calls.push({ signal: opts?.signal, resolve })
          opts?.signal?.addEventListener("abort", () => {
            const err = new Error("aborted")
            err.name = "AbortError"
            reject(err)
          })
        }),
    )

    const { result } = renderHook(() => useBackfillJobs(1))
    // Fetch inicial (efeito de montagem) — ainda pendurado (nunca resolvido).
    expect(calls).toHaveLength(1)
    expect(calls[0].signal?.aborted).toBe(false)

    act(() => {
      result.current.refetch()
    })
    // O refetch cria um 2º fetch, e ABORTA o controller do 1º — sem isto, o
    // 1º fetch (potencialmente mais lento) podia resolver DEPOIS do 2º e
    // regravar `items` com dado obsoleto.
    expect(calls).toHaveLength(2)
    expect(calls[0].signal?.aborted).toBe(true)
    expect(calls[1].signal?.aborted).toBe(false)

    // O 2º fetch (novo, não abortado) resolve normalmente.
    await act(async () => {
      calls[1].resolve(LIST_RESPONSE)
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.items).toEqual([JOB_1]))
    // Erro nenhum — o `.catch` reconhece o AbortError do 1º fetch e descarta,
    // em vez de tratá-lo como falha de rede visível ao usuário.
    expect(result.current.error).toBeNull()
  })
})
