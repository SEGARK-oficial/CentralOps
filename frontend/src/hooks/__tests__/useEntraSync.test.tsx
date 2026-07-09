/**
 * Testes do hook useEntraSync (Fase 2B).
 * Cobre: carregamento inicial, syncNow com sucesso e erro,
 * clearFeedback, refresh de status e setTimeout de atualização.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest"
import { useEntraSync } from "@/hooks/useEntraSync"
import * as api from "@/services/api"
import type { EntraSyncStatus, EntraSyncTriggerResult } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const mockStatus: EntraSyncStatus = {
  last_sync_at: "2026-06-14T09:00:00Z",
  last_sync_status: "ok",
  last_sync_summary: {
    created: 3,
    updated: 1,
    deactivated: 0,
    errors: [],
    started_at: "2026-06-14T09:00:00Z",
    finished_at: "2026-06-14T09:00:10Z",
  },
  lock_active: false,
}

const mockTriggerResult: EntraSyncTriggerResult = {
  queued: true,
  message: "Sync de usuários Entra disparado",
  lock_active: false,
}

describe("useEntraSync", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.getEntraSyncStatus.mockResolvedValue(mockStatus)
    mockedApi.syncEntraNow.mockResolvedValue(mockTriggerResult)
  })

  afterEach(() => {
    // Restaura timers reais caso algum teste os tenha substituído
    vi.useRealTimers()
  })

  it("carrega o status ao montar", async () => {
    const { result } = renderHook(() => useEntraSync())
    expect(result.current.loadingStatus).toBe(true)

    await waitFor(() => {
      expect(result.current.loadingStatus).toBe(false)
    })

    expect(result.current.syncStatus).toEqual(mockStatus)
    expect(mockedApi.getEntraSyncStatus).toHaveBeenCalledTimes(1)
  })

  it("estado inicial não tem feedback", async () => {
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))
    expect(result.current.feedback).toBeNull()
  })

  it("syncNow dispara sync e seta feedback de sucesso", async () => {
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    await act(async () => {
      await result.current.syncNow()
    })

    expect(mockedApi.syncEntraNow).toHaveBeenCalledTimes(1)
    expect(result.current.feedback).toEqual({
      type: "success",
      message: mockTriggerResult.message,
    })
  })

  it("syncNow retorna o resultado da API", async () => {
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    let returnValue: EntraSyncTriggerResult | null = null
    await act(async () => {
      returnValue = await result.current.syncNow()
    })

    expect(returnValue).toEqual(mockTriggerResult)
  })

  it("syncNow seta feedback info quando queued=false", async () => {
    mockedApi.syncEntraNow.mockResolvedValue({
      queued: false,
      message: "sync desabilitado na configuração",
      lock_active: false,
    })
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    await act(async () => {
      await result.current.syncNow()
    })

    expect(result.current.feedback?.type).toBe("info")
  })

  it("syncNow seta feedback de erro em caso de falha da API", async () => {
    mockedApi.syncEntraNow.mockRejectedValue(new Error("Broker indisponível"))
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    await act(async () => {
      await result.current.syncNow()
    })

    expect(result.current.feedback).toEqual({
      type: "error",
      message: "Broker indisponível",
    })
  })

  it("syncNow retorna null em caso de falha", async () => {
    mockedApi.syncEntraNow.mockRejectedValue(new Error("fail"))
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    let returnValue: EntraSyncTriggerResult | null = "not-null" as unknown as null
    await act(async () => {
      returnValue = await result.current.syncNow()
    })

    expect(returnValue).toBeNull()
  })

  it("syncNow dispara refreshStatus após 2s via setTimeout", async () => {
    // Usa fake timers apenas para este caso de teste
    vi.useFakeTimers()
    mockedApi.getEntraSyncStatus.mockResolvedValue(mockStatus)
    mockedApi.syncEntraNow.mockResolvedValue(mockTriggerResult)

    const { result } = renderHook(() => useEntraSync())

    // Resolve a promessa de carregamento inicial com fake timers
    await act(async () => {
      await Promise.resolve()
    })

    const callsBefore = mockedApi.getEntraSyncStatus.mock.calls.length

    await act(async () => {
      await result.current.syncNow()
    })

    // Antes dos 2s, não deve ter chamado de novo
    expect(mockedApi.getEntraSyncStatus).toHaveBeenCalledTimes(callsBefore)

    // Avança o timer e resolve promessas pendentes
    await act(async () => {
      vi.advanceTimersByTime(2000)
      await Promise.resolve()
    })

    expect(mockedApi.getEntraSyncStatus).toHaveBeenCalledTimes(callsBefore + 1)
    vi.useRealTimers()
  })

  it("clearFeedback limpa o feedback", async () => {
    mockedApi.syncEntraNow.mockRejectedValue(new Error("falha"))
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    await act(async () => {
      await result.current.syncNow()
    })
    expect(result.current.feedback).not.toBeNull()

    act(() => {
      result.current.clearFeedback()
    })
    expect(result.current.feedback).toBeNull()
  })

  it("refreshStatus recarrega o status", async () => {
    const { result } = renderHook(() => useEntraSync())
    await waitFor(() => expect(result.current.loadingStatus).toBe(false))

    const updatedStatus: EntraSyncStatus = { ...mockStatus, last_sync_status: "error", lock_active: false }
    mockedApi.getEntraSyncStatus.mockResolvedValue(updatedStatus)

    await act(async () => {
      await result.current.refreshStatus()
    })

    expect(result.current.syncStatus).toEqual(updatedStatus)
  })

  it("falha no carregamento inicial não propaga exceção", async () => {
    mockedApi.getEntraSyncStatus.mockRejectedValue(new Error("network error"))
    const { result } = renderHook(() => useEntraSync())

    await waitFor(() => expect(result.current.loadingStatus).toBe(false))
    // syncStatus fica null, sem throw
    expect(result.current.syncStatus).toBeNull()
  })
})
