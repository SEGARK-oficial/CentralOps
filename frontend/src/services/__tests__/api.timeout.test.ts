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
import { DEFAULT_REQUEST_TIMEOUT_MS } from "@/services/api/_core"

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
  it("requisição pendurada rejeita com TimeoutError após o timeout padrão", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    const promise = getAuthStatus()
    // Precisa capturar a expectativa ANTES de o timer disparar — a rejeição
    // acontece de forma síncrona ao evento `abort`, dentro do mesmo
    // `advanceTimersByTimeAsync`.
    const assertion = expect(promise).rejects.toMatchObject({ name: "TimeoutError" })

    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS)
    await assertion
  })

  it("a mensagem do TimeoutError é a i18n (não um literal cru inventado)", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    const promise = getAuthStatus()
    const assertion = expect(promise).rejects.toMatchObject({
      message: "A requisição demorou demais e foi cancelada. Tente de novo.",
    })

    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS)
    await assertion
  })

  it("não dispara timeout antes da hora — só depois", async () => {
    vi.useFakeTimers()
    mockHangingFetch()

    let settled = false
    const promise = getAuthStatus().catch(() => {
      settled = true
    })

    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS - 100)
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

describe("apiRequest — timeout coerente com o proxy e com upload", () => {
  it("o timeout padrão fica ACIMA do proxy_read_timeout do nginx (o proxy responde primeiro)", async () => {
    const { readFileSync } = await import("node:fs")
    for (const conf of ["nginx.single.conf", "nginx.single.https.conf"]) {
      const txt = readFileSync(conf, "utf8")
      const values = [...txt.matchAll(/proxy_read_timeout\s+(\d+)s;/g)].map((m) => Number(m[1]))
      // Positivo: o conf declara o timeout (senão o assert abaixo passaria vazio).
      expect(values.length).toBeGreaterThan(0)
      expect(DEFAULT_REQUEST_TIMEOUT_MS).toBeGreaterThan(Math.max(...values) * 1000)
    }
  })

  it("upload (FormData) não sofre o timeout padrão", async () => {
    vi.useFakeTimers()
    const fetchMock = mockHangingFetch()
    const { apiRequest } = await import("@/services/api/_core")

    let settled = false
    const body = new FormData()
    body.append("file", new Blob(["a,b\n1,2"]), "t.csv")
    void apiRequest("/upload", { method: "POST", body }).catch(() => {
      settled = true
    })

    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS * 2)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(settled).toBe(false)
  })

  it("403 redireciona para uma rota que existe (não para a antiga /search removida)", async () => {
    const { ADMIN_REDIRECT_PATH } = await import("@/services/api/_core")
    const { readFileSync } = await import("node:fs")
    const app = readFileSync("src/App.tsx", "utf8")
    const segment = ADMIN_REDIRECT_PATH.replace(/^\//, "")
    expect(segment.length).toBeGreaterThan(0)
    expect(app).toContain(`path="${segment}"`)
    expect(ADMIN_REDIRECT_PATH).not.toBe("/search")
  })
})
