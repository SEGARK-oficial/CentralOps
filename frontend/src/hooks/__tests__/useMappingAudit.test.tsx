/**
 * Testes de useMappingAudit.
 *
 * Cobre regressão crítica: o backend retorna envelope paginado
 * `{total, items, limit, offset}` mas o hook precisa expor `entries` como
 * array. Bug histórico: `entries.map is not a function` quando o service
 * não fazia unwrap → tela /mappings/{id} aba Auditoria ficava em branco.
 */

import { renderHook, waitFor } from "@testing-library/react"
import { useMappingAudit } from "@/hooks/useMappingAudit"
import * as api from "@/services/api"
import type { MappingAuditEntry } from "@/types"

vi.mock("@/services/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return {
    ...actual,
    getMappingAudit: vi.fn(),
  }
})
const mockedApi = vi.mocked(api)

const ENTRIES: MappingAuditEntry[] = [
  {
    id: "a1",
    mapping_definition_id: "m1",
    mapping_version_id: "v1",
    action: "create_version",
    user_id: 1,
    username: "alice",
    user_role: "engineer",
    diff: null,
    detail: "Criou versão v1",
    created_at: "2026-01-01T00:00:00Z",
  },
]

beforeEach(() => {
  vi.clearAllMocks()
})

describe("useMappingAudit", () => {
  it("expõe entries como array quando service retorna entries", async () => {
    mockedApi.getMappingAudit.mockResolvedValue({
      items: ENTRIES,
      total: ENTRIES.length,
      availableActions: ["create_version", "rollback"],
    })
    const { result } = renderHook(() => useMappingAudit("m1"))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(Array.isArray(result.current.entries)).toBe(true)
    expect(result.current.entries).toEqual(ENTRIES)
    expect(result.current.error).toBeNull()
  })

  it("zera entries em erro de rede e expõe error", async () => {
    mockedApi.getMappingAudit.mockRejectedValue(new Error("ECONNRESET"))
    const { result } = renderHook(() => useMappingAudit("m1"))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.entries).toEqual([])
    expect(result.current.error?.message).toBe("ECONNRESET")
  })

  it("entries sempre é array — protege contra service retornando undefined/null", async () => {
    // @ts-expect-error — exercitando contrato defensivo
    mockedApi.getMappingAudit.mockResolvedValue(undefined)
    const { result } = renderHook(() => useMappingAudit("m1"))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    // O service já faz unwrap defensivo; mas o hook NÃO deve quebrar mesmo
    // se receber valor inesperado. Esse contrato é exercido pela tabela
    // que faz `.map()` em entries.
    expect(() => result.current.entries.map((e) => e.id)).not.toThrow()
  })

  it("expõe availableActions servidas pelo backend", async () => {
    mockedApi.getMappingAudit.mockResolvedValue({
      items: ENTRIES,
      total: 1,
      availableActions: ["create_version", "rollback", "ignore_field"],
    })
    const { result } = renderHook(() => useMappingAudit("m1"))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.availableActions).toEqual([
      "create_version",
      "rollback",
      "ignore_field",
    ])
  })

  it("preserva availableActions em erro de rede", async () => {
    // O seletor de filtro não pode sumir da tela no meio de um erro — o
    // operador perderia o filtro que estava usando.
    mockedApi.getMappingAudit.mockResolvedValueOnce({
      items: ENTRIES,
      total: 1,
      availableActions: ["create_version", "rollback"],
    })
    const { result, rerender } = renderHook(
      ({ user }: { user?: string }) => useMappingAudit("m1", { username: user }),
      { initialProps: {} as { user?: string } },
    )
    await waitFor(() => expect(result.current.availableActions).toHaveLength(2))

    mockedApi.getMappingAudit.mockRejectedValueOnce(new Error("ECONNRESET"))
    rerender({ user: "bob" })

    await waitFor(() => expect(result.current.error).not.toBeNull())
    expect(result.current.entries).toEqual([])
    expect(result.current.availableActions).toEqual(["create_version", "rollback"])
  })
})
