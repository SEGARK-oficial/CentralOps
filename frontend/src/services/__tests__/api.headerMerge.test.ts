/**
 * R2-6.9 — `apiRequest` mesclava `headers` (default + chamador) e depois
 * espalhava `...requestOptions` DEPOIS, cujo próprio `headers` (não
 * mesclado) sobrescrevia o merge inteiro. Quem passasse `headers` próprios
 * perdia `Content-Type`/`Accept-Language` em silêncio.
 *
 * `getIntegrationHealth` é a chamada real mais simples que passa `headers`
 * pro `apiRequest` (`{ headers: V1_ACCEPT_HEADER }`, sem mais nenhuma outra
 * opção) — serve de sonda direta pro bug sem precisar exportar `apiRequest`.
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { getIntegrationHealth } from "@/services/api"

function mockFetchOk(body: unknown) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => body,
  } as unknown as Response)
  vi.stubGlobal("fetch", fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("apiRequest — merge de headers (R2-6.9)", () => {
  it("preserva Content-Type/Accept-Language default JUNTO com o header custom do chamador", async () => {
    const fetchMock = mockFetchOk({ status: "healthy" })

    await getIntegrationHealth(42)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>

    // Header custom da chamada (V1_ACCEPT_HEADER) sobrevive…
    expect(headers.Accept).toBe("application/vnd.centralops.v1+json")
    // …E os defaults do apiRequest não somem por causa dele.
    expect(headers["Content-Type"]).toBe("application/json")
    expect(headers["Accept-Language"]).toBeTruthy()
  })
})
