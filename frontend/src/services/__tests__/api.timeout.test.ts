/**
 * R4-6.1 — `apiRequest` sem timeout: uma requisição pendurada (proxy
 * travado, backend catatônico) nunca resolve nem rejeita, e trava
 * `inFlightRef`/poll de quem chamou pra sempre (ex.: `useBackfillJobs`).
 *
 * `getAuthStatus` é a chamada real mais simples que passa por `apiRequest`
 * sem opções — serve de sonda direta sem precisar exportar `apiRequest`.
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { getAuthStatus } from "@/services/api"

/** fetch que nunca resolve sozinho — só reage ao abort do `signal`, como o
 * `fetch` nativo faz de verdade. */
function mockHangingFetch() {
  const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
    return new Promise((_resolve, reject) => {
      const signal = init?.signal
      if (!signal) return
      if (signal.aborted) {
        reject(signal.reason)
        return
      }
      signal.addEventListener("abort", () => reject(signal.reason), { once: true })
    })
  })
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe("apiRequest — timeout (R4-6.1)", () => {
  it("requisição pendurada rejeita com TimeoutError após o timeout padrão (30s)", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    const promise = getAuthStatus()
    // Precisa capturar a expectativa ANTES de o timer disparar — a rejeição
    // acontece de forma síncrona ao evento `abort`, dentro do mesmo
    // `advanceTimersByTimeAsync`.
    const assertion = expect(promise).rejects.toMatchObject({ name: "TimeoutError" })

    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
  })

  it("a mensagem do TimeoutError é a i18n (não um literal cru inventado)", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    const promise = getAuthStatus()
    const assertion = expect(promise).rejects.toMatchObject({
      message: "A requisição demorou demais e foi cancelada. Tente de novo.",
    })

    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
  })

  it("não dispara timeout antes da hora (29.9s) — só depois (30s)", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    let settled = false
    const promise = getAuthStatus().catch(() => {
      settled = true
    })

    await vi.advanceTimersByTimeAsync(29_900)
    expect(settled).toBe(false)

    await vi.advanceTimersByTimeAsync(200)
    await promise
    expect(settled).toBe(true)
  })

  it("abort do CHAMADOR (não o timeout) continua rejeitando com AbortError, não TimeoutError", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    // `getAuthStatus` não aceita `signal` próprio — usa um caller real que
    // aceita, pra provar que o motivo original do abort do CHAMADOR
    // sobrevive à combinação com o signal do timeout.
    const { getMapping } = await import("@/services/api")
    const controller = new AbortController()
    const mappingPromise = getMapping("m1", { signal: controller.signal })
    const assertion = expect(mappingPromise).rejects.toMatchObject({ name: "AbortError" })

    controller.abort()
    await assertion
  })
})
