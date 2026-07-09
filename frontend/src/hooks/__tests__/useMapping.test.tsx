/**
 * Testes de useMapping
 * Cobre: sucesso, 404, erro de rede, refetch.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useMapping } from "@/hooks/useMapping"
import * as api from "@/services/api"
import { ApiRequestError } from "@/services/api"
import type { Mapping, MappingVersion } from "@/types"

// Mock só os getters; preserva ApiRequestError real (class importada acima
// é substituída por undefined se o mock for total).
vi.mock("@/services/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    getMapping: vi.fn(),
  }
})
const mockedApi = vi.mocked(api)

const VERSION: MappingVersion = {
  id: "v1",
  definition_id: "m1",
  version_number: 1,
  rules: { preprocess: [], rules: [{ target: "event.action", source: "action" }] },
  author_user_id: null,
  commit_message: "Versão inicial",
  diff_from_previous: null,
  dry_run_stats: null,
  created_at: "2026-01-01T00:00:00Z",
}

const MAPPING: Mapping & { versions: MappingVersion[] } = {
  id: "m1",
  vendor: "wazuh",
  event_type: "authentication",
  description: "Autenticação",
  current_version_id: "v1",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  versions: [VERSION],
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("useMapping", () => {
  it("retorna data após sucesso", async () => {
    mockedApi.getMapping.mockResolvedValue(MAPPING)

    const { result } = renderHook(() => useMapping("m1"))

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.data).toEqual(MAPPING)
    expect(result.current.error).toBeNull()
  })

  it("popula error em 404", async () => {
    mockedApi.getMapping.mockRejectedValue(
      new ApiRequestError("Mapping não encontrado", 404),
    )

    const { result } = renderHook(() => useMapping("nao-existe"))

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.data).toBeNull()
    expect(result.current.error).toBeInstanceOf(ApiRequestError)
    expect((result.current.error as ApiRequestError).statusCode).toBe(404)
  })

  it("popula error em erro de rede", async () => {
    mockedApi.getMapping.mockRejectedValue(new Error("Network error"))

    const { result } = renderHook(() => useMapping("m1"))

    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error?.message).toBe("Network error")
  })

  it("refetch re-executa a busca", async () => {
    mockedApi.getMapping.mockResolvedValue(MAPPING)

    const { result } = renderHook(() => useMapping("m1"))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.getMapping).toHaveBeenCalledTimes(1)

    act(() => result.current.refetch())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.getMapping).toHaveBeenCalledTimes(2)
  })
})
