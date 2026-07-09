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
    action: "version_created",
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
    mockedApi.getMappingAudit.mockResolvedValue(ENTRIES)
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
})
