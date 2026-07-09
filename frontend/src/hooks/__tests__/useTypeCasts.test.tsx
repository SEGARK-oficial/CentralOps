/**
 * Testes de useTypeCasts
 * Cobre: loading→data, cache hit na segunda montagem, abort no unmount, erro 500.
 */

import { renderHook, waitFor } from "@testing-library/react"
import { useTypeCasts, _resetTypeCastsCache } from "@/hooks/useTypeCasts"
import * as api from "@/services/api"

// Mock parcial preservando ApiRequestError para outros testes que importam api.
vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return { ...actual, fetchTypeCasts: vi.fn() }
})
const mockedApi = vi.mocked(api)

const TWELVE_CASTS = [
  { name: "dedup", description: "Remove duplicatas", signature: "dedup(value: any) -> any" },
  { name: "epoch_to_iso", description: "Epoch ms para ISO 8601", signature: "epoch_to_iso(ts: int) -> str" },
  { name: "iso_to_epoch", description: "ISO 8601 para epoch ms", signature: "iso_to_epoch(ts: str) -> int" },
  { name: "lowercase", description: "Converte para minúsculas", signature: "lowercase(s: str) -> str" },
  { name: "mitre_tactic_to_ocsf", description: "MITRE tactic para OCSF", signature: "mitre_tactic_to_ocsf(tactic: str) -> int" },
  { name: "score_to_percent", description: "Normaliza score para percentual", signature: "score_to_percent(score: float) -> float" },
  { name: "to_array", description: "Converte para array", signature: "to_array(value: any) -> list" },
  { name: "to_bool", description: "Coerce para bool", signature: "to_bool(value: any) -> bool" },
  { name: "to_int", description: "Coerce para inteiro", signature: "to_int(value: any) -> int" },
  { name: "to_str", description: "Coerce para string", signature: "to_str(value: any) -> str" },
  { name: "trim", description: "Remove espaços nas bordas", signature: "trim(s: str) -> str" },
  { name: "uppercase", description: "Converte para maiúsculas", signature: "uppercase(s: str) -> str" },
]

beforeEach(() => {
  vi.clearAllMocks()
  // Cada teste começa com cache limpo para isolar comportamento.
  _resetTypeCastsCache()
})

describe("useTypeCasts — primeiro fetch", () => {
  it("inicia com loading=true e data=null, depois retorna os 12 casts", async () => {
    mockedApi.fetchTypeCasts.mockResolvedValue(TWELVE_CASTS)

    const { result } = renderHook(() => useTypeCasts())

    // Estado inicial: loading
    expect(result.current.loading).toBe(true)
    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeNull()

    // Após resolução
    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.data).toHaveLength(12)
    expect(result.current.data![0].name).toBe("dedup")
    expect(result.current.data![2].name).toBe("iso_to_epoch")
    expect(result.current.error).toBeNull()
    expect(mockedApi.fetchTypeCasts).toHaveBeenCalledTimes(1)
  })

  it("contém todos os 12 nomes esperados", async () => {
    mockedApi.fetchTypeCasts.mockResolvedValue(TWELVE_CASTS)
    const { result } = renderHook(() => useTypeCasts())
    await waitFor(() => expect(result.current.loading).toBe(false))

    const names = result.current.data!.map((c) => c.name)
    expect(names).toEqual([
      "dedup", "epoch_to_iso", "iso_to_epoch", "lowercase",
      "mitre_tactic_to_ocsf", "score_to_percent", "to_array", "to_bool",
      "to_int", "to_str", "trim", "uppercase",
    ])
  })
})

describe("useTypeCasts — cache", () => {
  it("segunda montagem usa cache: fetchTypeCasts não é chamado novamente", async () => {
    mockedApi.fetchTypeCasts.mockResolvedValue(TWELVE_CASTS)

    // Primeira montagem — popula cache
    const { result: r1, unmount: u1 } = renderHook(() => useTypeCasts())
    await waitFor(() => expect(r1.current.loading).toBe(false))
    expect(mockedApi.fetchTypeCasts).toHaveBeenCalledTimes(1)
    u1()

    // Segunda montagem — deve usar cache sem novo fetch
    const { result: r2 } = renderHook(() => useTypeCasts())

    // Cache hit é síncrono: loading=false imediatamente
    expect(r2.current.loading).toBe(false)
    expect(r2.current.data).toHaveLength(12)
    expect(mockedApi.fetchTypeCasts).toHaveBeenCalledTimes(1) // não aumentou
  })
})

describe("useTypeCasts — unmount durante fetch", () => {
  it("abort no unmount: sem warning de setState em componente desmontado", async () => {
    let aborted = false

    mockedApi.fetchTypeCasts.mockImplementation((_opts) => {
      return new Promise((_res, rej) => {
        _opts?.signal?.addEventListener("abort", () => {
          aborted = true
          rej(new DOMException("Aborted", "AbortError"))
        })
      })
    })

    const { result, unmount } = renderHook(() => useTypeCasts())
    expect(result.current.loading).toBe(true)

    unmount()

    // Aguarda microtask para garantir que o abort foi processado
    await new Promise((r) => setTimeout(r, 20))

    expect(aborted).toBe(true)
    // Não deve ter disparado error (AbortError é ignorado)
    expect(result.current.error).toBeNull()
  })
})

describe("useTypeCasts — erro de API", () => {
  it("erro 500: error populado, loading=false, data=null", async () => {
    const serverError = new Error("Internal Server Error")
    mockedApi.fetchTypeCasts.mockRejectedValue(serverError)

    const { result } = renderHook(() => useTypeCasts())

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeInstanceOf(Error)
    expect(result.current.error!.message).toBe("Internal Server Error")
  })

  it("erro genérico não-Error: wrappado em Error", async () => {
    mockedApi.fetchTypeCasts.mockRejectedValue("falha inesperada")

    const { result } = renderHook(() => useTypeCasts())

    await waitFor(() => expect(result.current.loading).toBe(false))

    expect(result.current.error).toBeInstanceOf(Error)
    expect(result.current.error!.message).toBe("falha inesperada")
  })
})
