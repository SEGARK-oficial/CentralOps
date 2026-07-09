/**
 * Testes de useQuarantine
 * Cobre: GET sucesso, discard + refetch, getDetail, erro de rede, paginação.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useQuarantine } from "@/hooks/useQuarantine"
import * as api from "@/services/api"
import type { QuarantineDetail, QuarantineEntry } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    listQuarantine: vi.fn(),
    discardQuarantine: vi.fn(),
    getQuarantineDetail: vi.fn(),
    reprocessQuarantine: vi.fn(),
  }
})
const mockedApi = vi.mocked(api)

const ENTRY: QuarantineEntry = {
  id: "q1",
  integration_id: 1,
  vendor: "sophos",
  event_type: "endpoint.threat",
  error_kind: "schema_error",
  error_detail: "Field 'user' is required",
  mapping_version_id: "mv1",
  created_at: "2026-01-01T00:00:00Z",
  expires_at: "2026-02-01T00:00:00Z",
  reprocessed_at: null,
}

const DETAIL: QuarantineDetail = {
  ...ENTRY,
  raw_payload: { event: "endpoint.threat", user: null },
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

describe("useQuarantine", () => {
  it("retorna items e total após GET bem-sucedido", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() => useQuarantine({}))

    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.items).toEqual([ENTRY])
    expect(result.current.total).toBe(1)
    expect(result.current.error).toBeNull()
  })

  it("popula error em caso de falha de rede", async () => {
    mockedApi.listQuarantine.mockRejectedValue(new Error("Network error"))

    const { result } = renderHook(() => useQuarantine({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.error?.message).toBe("Network error")
    expect(result.current.items).toEqual([])
  })

  it("repassa filtros como query params", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() =>
      useQuarantine({ vendor: "sophos", error_kind: "schema_error", limit: 5 }),
    )
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listQuarantine).toHaveBeenCalledWith(
      expect.objectContaining({ vendor: "sophos", error_kind: "schema_error", limit: 5 }),
      expect.any(Object),
    )
  })

  it("refetch re-executa a busca", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)

    const { result } = renderHook(() => useQuarantine({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listQuarantine).toHaveBeenCalledTimes(1)

    act(() => result.current.refetch())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listQuarantine).toHaveBeenCalledTimes(2)
  })

  it("discard chama discardQuarantine e faz refetch", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)
    mockedApi.discardQuarantine.mockResolvedValue(undefined)

    const { result } = renderHook(() => useQuarantine({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await act(async () => {
      await result.current.discard("q1")
    })

    expect(mockedApi.discardQuarantine).toHaveBeenCalledWith("q1")
    expect(mockedApi.listQuarantine).toHaveBeenCalledTimes(2)
  })

  it("getDetail retorna QuarantineDetail sem fazer refetch", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)
    mockedApi.getQuarantineDetail.mockResolvedValue(DETAIL)

    const { result } = renderHook(() => useQuarantine({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    const calls1 = mockedApi.listQuarantine.mock.calls.length

    let detail: QuarantineDetail | undefined
    await act(async () => {
      detail = await result.current.getDetail("q1")
    })

    expect(mockedApi.getQuarantineDetail).toHaveBeenCalledWith("q1")
    expect(detail).toEqual(DETAIL)
    // getDetail NÃO faz refetch da lista
    expect(mockedApi.listQuarantine).toHaveBeenCalledTimes(calls1)
  })

  it("paginação: repassa limit e offset", async () => {
    mockedApi.listQuarantine.mockResolvedValue({ ...LIST_RESPONSE, total: 50 })

    const { result } = renderHook(() => useQuarantine({ limit: 10, offset: 30 }))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(mockedApi.listQuarantine).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 10, offset: 30 }),
      expect.any(Object),
    )
  })

  it("reprocess chama API e refetch", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)
    const updatedEntry: QuarantineEntry = { ...ENTRY, reprocessed_at: "2026-04-25T10:00:00Z" }
    mockedApi.reprocessQuarantine.mockResolvedValue(updatedEntry)

    const { result } = renderHook(() => useQuarantine({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    let updated: QuarantineEntry | undefined
    await act(async () => {
      updated = await result.current.reprocess("q1")
    })

    expect(mockedApi.reprocessQuarantine).toHaveBeenCalledWith("q1")
    expect(updated).toEqual(updatedEntry)
    // refetch triggered — list called a second time
    expect(mockedApi.listQuarantine).toHaveBeenCalledTimes(2)
  })

  it("reprocess propaga erro 422", async () => {
    mockedApi.listQuarantine.mockResolvedValue(LIST_RESPONSE)
    const err = Object.assign(new Error("Mapping ainda falha: rule required"), { statusCode: 422 })
    mockedApi.reprocessQuarantine.mockRejectedValue(err)

    const { result } = renderHook(() => useQuarantine({}))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    await expect(
      act(async () => {
        await result.current.reprocess("q1")
      }),
    ).rejects.toMatchObject({ message: "Mapping ainda falha: rule required", statusCode: 422 })
  })

  it("AbortError não popula error", async () => {
    mockedApi.listQuarantine.mockImplementation((_filters, opts) =>
      new Promise((_res, rej) => {
        opts?.signal?.addEventListener("abort", () =>
          rej(new DOMException("Aborted", "AbortError")),
        )
      }),
    )

    const { result, unmount } = renderHook(() => useQuarantine({}))
    unmount()

    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })

    expect(result.current.error).toBeNull()
  })
})
