/**
 * Testes de useDrift
 * Cobre: GET sucesso, mutations chamam endpoints corretos,
 *        refetch após mutation, paginação, erro de rede.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useDrift } from "@/hooks/useDrift"
import * as api from "@/services/api"
import type { DriftEntry } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listDrift: vi.fn(),
    ignoreDrift: vi.fn(),
    markDriftMapped: vi.fn(),
    deleteDrift: vi.fn(),
  }
})
const mockedApi = vi.mocked(api)

const ENTRY: DriftEntry = {
  id: "d1",
  vendor: "wazuh",
  event_type: "authentication",
  field_path: "extra.custom_field",
  sample_value: "test",
  sample_type: "string",
  occurrence_count: 5,
  first_seen: "2026-01-01T00:00:00Z",
  last_seen: "2026-01-02T00:00:00Z",
  status: "new",
}

const LIST_RESPONSE = {
  items: [ENTRY],
  total: 1,
  limit: 20,
  offset: 0,
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("useDrift", () => {
  it("retorna items e total após GET bem-sucedido", async () => {
    mockedApi.listDrift.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() => useDrift({}))

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.items).toEqual([ENTRY])
    expect(result.current.total).toBe(1)
    expect(result.current.error).toBeNull()
  })

  it("popula error em caso de falha de rede", async () => {
    mockedApi.listDrift.mockRejectedValue(new Error("Network error"))

    const { result } = renderHook(() => useDrift({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error?.message).toBe("Network error")
    expect(result.current.items).toEqual([])
    expect(result.current.total).toBe(0)
  })

  it("repassa filtros como query params ao listar", async () => {
    mockedApi.listDrift.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() =>
      useDrift({ vendor: "wazuh", status: "new", limit: 10, offset: 0 }),
    )
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listDrift).toHaveBeenCalledWith(
      expect.objectContaining({ vendor: "wazuh", status: "new", limit: 10 }),
      expect.any(Object),
    )
  })

  it("refetch re-executa a busca", async () => {
    mockedApi.listDrift.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() => useDrift({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listDrift).toHaveBeenCalledTimes(1)

    act(() => result.current.refetch())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listDrift).toHaveBeenCalledTimes(2)
  })

  it("ignoreField chama ignoreDrift e faz refetch", async () => {
    mockedApi.listDrift.mockResolvedValue(LIST_RESPONSE)
    mockedApi.ignoreDrift.mockResolvedValue({ ...ENTRY, status: "ignored" })

    const { result } = renderHook(() => useDrift({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await result.current.ignoreField("d1")
    })

    expect(mockedApi.ignoreDrift).toHaveBeenCalledWith("d1")
    expect(mockedApi.listDrift).toHaveBeenCalledTimes(2) // initial + refetch
  })

  it("markMapped chama markDriftMapped e faz refetch", async () => {
    mockedApi.listDrift.mockResolvedValue(LIST_RESPONSE)
    mockedApi.markDriftMapped.mockResolvedValue({ ...ENTRY, status: "mapped" })

    const { result } = renderHook(() => useDrift({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await result.current.markMapped("d1")
    })

    expect(mockedApi.markDriftMapped).toHaveBeenCalledWith("d1")
    expect(mockedApi.listDrift).toHaveBeenCalledTimes(2)
  })

  it("deleteField chama deleteDrift e faz refetch", async () => {
    mockedApi.listDrift.mockResolvedValue(LIST_RESPONSE)
    mockedApi.deleteDrift.mockResolvedValue(undefined)

    const { result } = renderHook(() => useDrift({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await result.current.deleteField("d1")
    })

    expect(mockedApi.deleteDrift).toHaveBeenCalledWith("d1")
    expect(mockedApi.listDrift).toHaveBeenCalledTimes(2)
  })

  it("paginação: repassa limit e offset corretos", async () => {
    mockedApi.listDrift.mockResolvedValue({ ...LIST_RESPONSE, total: 100 })

    const { result } = renderHook(() => useDrift({ limit: 10, offset: 20 }))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listDrift).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 10, offset: 20 }),
      expect.any(Object),
    )
  })

  it("AbortError não popula error", async () => {
    mockedApi.listDrift.mockImplementation((_filters, opts) =>
      new Promise((_res, rej) => {
        opts?.signal?.addEventListener("abort", () =>
          rej(new DOMException("Aborted", "AbortError")),
        )
      }),
    )

    const { result, unmount } = renderHook(() => useDrift({}))
    unmount()

    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })

    expect(result.current.error).toBeNull()
  })
})
