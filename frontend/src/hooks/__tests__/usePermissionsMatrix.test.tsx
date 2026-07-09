import { renderHook, waitFor } from "@testing-library/react"
import { usePermissionsMatrix, clearPermissionsMatrixCache } from "@/hooks/usePermissionsMatrix"
import * as api from "@/services/api"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const fakeMatrix = {
  viewer: ["mapping.read"],
  operator: ["mapping.read", "drift.ignore"],
  engineer: ["mapping.read", "mapping.write", "mapping.rollback"],
  admin: ["mapping.read", "mapping.write", "mapping.rollback", "user.manage"],
}

describe("usePermissionsMatrix", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    clearPermissionsMatrixCache()
    mockedApi.getPermissionsMatrix.mockResolvedValue(fakeMatrix)
  })

  it("carrega matriz na montagem", async () => {
    const { result } = renderHook(() => usePermissionsMatrix())
    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.matrix).toEqual(fakeMatrix)
    expect(result.current.error).toBeNull()
  })

  it("expõe erro quando api falha", async () => {
    mockedApi.getPermissionsMatrix.mockRejectedValue(new Error("Falha de rede"))
    const { result } = renderHook(() => usePermissionsMatrix())
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.error?.message).toBe("Falha de rede")
    expect(result.current.matrix).toBeNull()
  })

  it("segunda instância usa cache sem nova chamada de API", async () => {
    // Primeira instância carrega e popula cache
    const { result: r1 } = renderHook(() => usePermissionsMatrix())
    await waitFor(() => expect(r1.current.isLoading).toBe(false))
    expect(mockedApi.getPermissionsMatrix).toHaveBeenCalledTimes(1)

    // Segunda instância: já parte com cache populado (isLoading=false imediato)
    const { result: r2 } = renderHook(() => usePermissionsMatrix())
    // Como cache está preenchido, isLoading já é false na inicialização
    expect(r2.current.isLoading).toBe(false)
    expect(r2.current.matrix).toEqual(fakeMatrix)

    // API não foi chamada uma segunda vez
    expect(mockedApi.getPermissionsMatrix).toHaveBeenCalledTimes(1)
  })
})
