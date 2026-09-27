/**
 * Testes de useBackfillJobs
 * Cobre: list, create, cancel, polling, refetch, erro de rede.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useBackfillJobs } from "@/hooks/useBackfillJobs"
import * as api from "@/services/api"
import type { BackfillJob, BackfillJobStatus } from "@/types"

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

// ── R3-5.2: abort só em troca de filtro/integração, nunca em poll silencioso ──
//
// Regressão da R2-5.4: o guard de abort ali abortava QUALQUER fetch em voo,
// inclusive a carga inicial — e a geração abortada nunca zerava `isLoading`
// (nem a nova, que era silenciosa), resultando em skeleton eterno. Os testes
// abaixo substituem a versão anterior desta suíte (que exigia — errado — que
// um `refetch()` silencioso abortasse a carga inicial).

interface PendingCall {
  signal?: AbortSignal | null
  resolve: (v: typeof LIST_RESPONSE) => void
}

function mockPendingListCalls(): PendingCall[] {
  const calls: PendingCall[] = []
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
  return calls
}

describe("useBackfillJobs — R3-5.2: poll/refetch silencioso NUNCA aborta uma request em voo", () => {
  it("um refetch silencioso durante a carga inicial PULA o tick (não aborta, não duplica)", async () => {
    const calls = mockPendingListCalls()

    const { result } = renderHook(() => useBackfillJobs(1))
    // Fetch inicial (efeito de montagem) — ainda pendurado (nunca resolvido).
    expect(calls).toHaveLength(1)
    expect(result.current.isLoading).toBe(true)

    act(() => {
      result.current.refetch()
    })
    // Silencioso (`showLoading=false`) + já em voo → PULA: nem aborta o 1º
    // nem cria um 2º. Antes (bug), isto abortava o 1º e travava o
    // `isLoading` em `true` pra sempre.
    expect(calls).toHaveLength(1)
    expect(calls[0].signal?.aborted).toBe(false)

    await act(async () => {
      calls[0].resolve(LIST_RESPONSE)
      await Promise.resolve()
    })
    await waitFor(() => expect(result.current.items).toEqual([JOB_1]))
    expect(result.current.isLoading).toBe(false)
  })

  // Teste explícito pedido pela Rodada 3: poll disparado durante a carga
  // inicial LENTA → isLoading termina `false` (não fica preso).
  it("poll disparado durante a carga inicial lenta → isLoading termina false", async () => {
    vi.useFakeTimers()
    try {
      const calls = mockPendingListCalls()

      const { result } = renderHook(() =>
        useBackfillJobs(1, undefined, { refreshIntervalMs: 5000 }),
      )
      expect(result.current.isLoading).toBe(true)
      expect(calls).toHaveLength(1)

      // O poll dispara ENQUANTO a carga inicial ainda está pendurada.
      await act(async () => {
        vi.advanceTimersByTime(5001)
        await Promise.resolve()
      })
      // Pulou o tick — continua só 1 request em voo, e `isLoading` ainda
      // reflete a carga inicial (de verdade em andamento).
      expect(calls).toHaveLength(1)
      expect(result.current.isLoading).toBe(true)

      // A carga inicial (lenta) finalmente resolve.
      await act(async () => {
        calls[0].resolve(LIST_RESPONSE)
        await Promise.resolve()
      })
      expect(result.current.isLoading).toBe(false)
      expect(result.current.items).toEqual([JOB_1])
    } finally {
      vi.useRealTimers()
    }
  })

  // Teste explícito pedido pela Rodada 3: troca de filtro durante a carga →
  // a resposta velha é descartada e o isLoading segue correto.
  it("troca de filtro durante a carga → a resposta velha é descartada e isLoading segue correto", async () => {
    const calls = mockPendingListCalls()

    const { result, rerender } = renderHook(
      ({ filters }: { filters?: { status: BackfillJobStatus } }) => useBackfillJobs(1, filters),
      { initialProps: { filters: { status: "pending" as BackfillJobStatus } } },
    )
    expect(calls).toHaveLength(1)
    expect(result.current.isLoading).toBe(true)

    // Troca de filtro ENQUANTO a 1ª carga (pending) ainda está em voo —
    // `showLoading=true` de novo (é a mesma carga "principal"), então PODE
    // abortar a geração anterior.
    rerender({ filters: { status: "completed" as BackfillJobStatus } })
    expect(calls).toHaveLength(2)
    expect(calls[0].signal?.aborted).toBe(true)
    expect(calls[1].signal?.aborted).toBe(false)
    expect(result.current.isLoading).toBe(true)

    // A resposta da geração NOVA chega primeiro.
    const completedJob: BackfillJob = { ...JOB_1, id: "completed-job", status: "completed" }
    await act(async () => {
      calls[1].resolve({ items: [completedJob], total: 1, limit: 50, offset: 0 })
      await Promise.resolve()
    })
    expect(result.current.isLoading).toBe(false)
    expect(result.current.items).toEqual([completedJob])

    // A resposta da geração VELHA (abortada) chega depois, fora de ordem —
    // não pode sobrescrever o resultado da troca de filtro, nem reabrir o
    // loading.
    await act(async () => {
      calls[0].resolve(LIST_RESPONSE)
      await Promise.resolve()
    })
    expect(result.current.items).toEqual([completedJob])
    expect(result.current.isLoading).toBe(false)
    expect(result.current.error).toBeNull()
  })
})

// R4-5.3 (regressão da R3-5.2): "pula em vez de aborta" era certo pro POLL,
// mas também descartava um refetch pós-mutação/manual que caísse durante um
// poll em voo — o job novo só aparecia no PRÓXIMO tick (até refreshIntervalMs
// depois), não na hora. Reverter a distinção "poll" vs. "refetch" em
// `fetchJobs` (voltar a tratar todo silencioso igual) faz os dois testes
// abaixo falharem de volta.
describe("useBackfillJobs — R4-5.3: mutação durante um poll em voo não espera o próximo tick", () => {
  it("createJob durante um poll em voo: o job novo aparece assim que o poll libera, sem esperar o intervalo inteiro", async () => {
    vi.useFakeTimers()
    try {
      const calls = mockPendingListCalls()
      const newJob: BackfillJob = { ...JOB_1, id: "bbbb-2222", status: "pending" }
      mockedApi.createBackfillJob.mockResolvedValue(newJob)

      const { result } = renderHook(() => useBackfillJobs(1, undefined, { refreshIntervalMs: 5000 }))
      // Carga inicial resolve normalmente.
      expect(calls).toHaveLength(1)
      await act(async () => {
        calls[0].resolve(LIST_RESPONSE)
        await Promise.resolve()
      })
      expect(result.current.isLoading).toBe(false)

      // O poll de rotina dispara e fica pendurado (em voo).
      await act(async () => {
        vi.advanceTimersByTime(5001)
        await Promise.resolve()
      })
      expect(calls).toHaveLength(2)

      // Enquanto o poll ainda está em voo, o usuário cria um job.
      let createPromise: Promise<BackfillJob> | undefined
      act(() => {
        createPromise = result.current.createJob({
          streams: ["alerts"],
          from_ts: "2026-01-01T00:00:00Z",
          to_ts: "2026-01-10T00:00:00Z",
        })
      })
      await act(async () => {
        await Promise.resolve()
      })
      expect(mockedApi.createBackfillJob).toHaveBeenCalled()
      // O refetch pós-createJob encontrou o poll em voo: NÃO cria uma 3ª
      // request nem aborta a do poll — só marca a intenção pendente.
      expect(calls).toHaveLength(2)

      // O poll em voo finalmente libera, trazendo a lista ANTIGA (sem o job
      // novo) — mas isto dispara, no `finally`, o refetch pendente.
      await act(async () => {
        calls[1].resolve(LIST_RESPONSE)
        await Promise.resolve()
      })
      // Sem o fix (R4-5.3), o teste pararia aqui: nenhuma 3ª request jamais
      // aconteceria, e o job novo só apareceria no PRÓXIMO tick de poll.
      expect(calls).toHaveLength(3)

      const jobWithNew = { items: [JOB_1, newJob], total: 2, limit: 50, offset: 0 }
      await act(async () => {
        calls[2].resolve(jobWithNew)
        await Promise.resolve()
      })
      await createPromise

      expect(result.current.items).toEqual([JOB_1, newJob])
      // Continua silencioso — a mutação nunca acendeu o skeleton de página
      // cheia (PERF-07).
      expect(result.current.isLoading).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it("botão de atualizar (refetch manual) durante um poll em voo também é honrado, não descartado", async () => {
    vi.useFakeTimers()
    try {
      const calls = mockPendingListCalls()

      const { result } = renderHook(() => useBackfillJobs(1, undefined, { refreshIntervalMs: 5000 }))
      expect(calls).toHaveLength(1)
      await act(async () => {
        calls[0].resolve(LIST_RESPONSE)
        await Promise.resolve()
      })

      await act(async () => {
        vi.advanceTimersByTime(5001)
        await Promise.resolve()
      })
      expect(calls).toHaveLength(2)

      // Usuário aperta "atualizar" enquanto o poll de rotina ainda está em voo.
      act(() => {
        result.current.refetch()
      })
      // Não descarta silenciosamente (bug do R4-5.3) nem duplica a request.
      expect(calls).toHaveLength(2)

      const updated = { items: [JOB_1], total: 1, limit: 50, offset: 0 }
      await act(async () => {
        calls[1].resolve(updated)
        await Promise.resolve()
      })
      // O refetch manual pendente dispara assim que o poll libera.
      expect(calls).toHaveLength(3)
    } finally {
      vi.useRealTimers()
    }
  })
})
