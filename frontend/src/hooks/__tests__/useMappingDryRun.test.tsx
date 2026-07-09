/**
 * Testes de useMappingDryRun (D3)
 * Cobre: debounce, AbortController, race condition, erro de rede, sem rules.
 */

import { renderHook, act, waitFor } from "@testing-library/react"
import { useMappingDryRun } from "@/hooks/useMappingDryRun"
import * as api from "@/services/api"
import type { MappingRule, DryRunResult } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const RULE: MappingRule = { target: "event.action", source: "action" }
const RULE_2: MappingRule = { target: "event.type", source: "type" }

const RESULT: DryRunResult = {
  sample_size: 10,
  ok_count: 8,
  fail_count: 2,
  rule_failures: [],
  output_examples: [{ event: { action: "login" } }],
  default_hit_warnings: [],
}

afterEach(() => {
  vi.clearAllMocks()
})

// ── Testes de state com timers reais ─────────────────────────────────────────

describe("useMappingDryRun — state", () => {
  it("sem rules: result=null e fetch não é disparado", async () => {
    const { result } = renderHook(() =>
      useMappingDryRun([], null, { debounceMs: 0 }),
    )

    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })

    expect(mockedApi.postMappingDryRun).not.toHaveBeenCalled()
    expect(result.current.result).toBeNull()
    expect(result.current.isPending).toBe(false)
  })

  it("com rules: dispara fetch e retorna result", async () => {
    mockedApi.postMappingDryRun.mockResolvedValue(RESULT)

    const rules = [RULE]
    const { result } = renderHook(() =>
      useMappingDryRun(rules, null),
    )

    await waitFor(() => expect(result.current.isPending).toBe(false))

    expect(mockedApi.postMappingDryRun).toHaveBeenCalled()
    expect(result.current.result).toEqual(RESULT)
    expect(result.current.error).toBeNull()
  })

  it("erro de rede: error populado e isPending=false", async () => {
    const netError = new Error("Network failure")
    mockedApi.postMappingDryRun.mockRejectedValue(netError)

    const rules = [RULE]
    const { result } = renderHook(() =>
      useMappingDryRun(rules, null),
    )

    await waitFor(() => expect(result.current.isPending).toBe(false))

    expect(result.current.error?.message).toBe("Network failure")
    expect(result.current.result).toBeNull()
  })

  it("AbortError (desmount → cleanup): error não é populado", async () => {
    mockedApi.postMappingDryRun.mockImplementation((_payload, opts) => {
      return new Promise((_res, rej) => {
        opts?.signal?.addEventListener("abort", () =>
          rej(new DOMException("Aborted", "AbortError")),
        )
      })
    })

    const { result, unmount } = renderHook(() =>
      useMappingDryRun([RULE], null, { debounceMs: 0 }),
    )

    // Desmonta → cleanup aborta o controller
    unmount()
    await act(async () => { await new Promise((r) => setTimeout(r, 20)) })

    expect(result.current.error).toBeNull()
  })

  it("race condition: segunda request (última) vence a primeira", async () => {
    const resolvers: Array<(v: DryRunResult) => void> = []

    mockedApi.postMappingDryRun.mockImplementation(() =>
      new Promise((res) => { resolvers.push(res) }),
    )

    const { rerender, result } = renderHook(
      ({ rules }: { rules: MappingRule[] }) =>
        useMappingDryRun(rules, null, { debounceMs: 0 }),
      { initialProps: { rules: [RULE] } },
    )

    // Aguarda até termos pelo menos 1 request em voo
    await act(async () => { await new Promise((r) => setTimeout(r, 10)) })

    // Muda rules → nova request; a anterior é abortada
    rerender({ rules: [RULE_2] })
    await act(async () => { await new Promise((r) => setTimeout(r, 10)) })

    // Resolve a última (índice mais alto)
    const last = resolvers[resolvers.length - 1]
    const secondResult: DryRunResult = { ...RESULT, ok_count: 99 }
    await act(async () => { last(secondResult) })

    await waitFor(() => expect(result.current.isPending).toBe(false))
    expect(result.current.result?.ok_count).toBe(99)
  })
})

// ── Testes de debounce (debounce agora é responsabilidade do caller) ─────────
//
// O hook não faz mais debounce interno — o caller (MappingEditorPage) passa
// `effectiveRules` já debounced. Os testes abaixo validam que a mudança de
// rules dispara um novo request imediatamente (sem atraso no hook).

describe("useMappingDryRun — sem debounce interno", () => {
  it("mudança de rules dispara request imediatamente (sem debounce)", async () => {
    mockedApi.postMappingDryRun.mockResolvedValue(RESULT)

    const { rerender } = renderHook(
      ({ rules }: { rules: MappingRule[] }) =>
        useMappingDryRun(rules, null),
      { initialProps: { rules: [RULE] } },
    )

    await act(async () => { await new Promise((r) => setTimeout(r, 10)) })
    const after1 = mockedApi.postMappingDryRun.mock.calls.length
    expect(after1).toBeGreaterThanOrEqual(1)

    // Muda rules → novo request imediato
    rerender({ rules: [RULE_2] })
    await act(async () => { await new Promise((r) => setTimeout(r, 10)) })

    expect(mockedApi.postMappingDryRun.mock.calls.length).toBeGreaterThan(after1)
  })

  it("debounceMs no options é aceito mas ignorado pelo hook", async () => {
    mockedApi.postMappingDryRun.mockResolvedValue(RESULT)

    // Passa debounceMs — hook aceita a option mas não a usa internamente
    // Verificamos apenas que o hook chama a API normalmente
    const rules = [RULE]
    const { result } = renderHook(() =>
      useMappingDryRun(rules, null, { debounceMs: 400 }),
    )

    await waitFor(() => expect(result.current.isPending).toBe(false))

    expect(mockedApi.postMappingDryRun).toHaveBeenCalled()
    expect(result.current.result).toEqual(RESULT)
  })
})
