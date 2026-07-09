/**
 * Testes de usePermission — Sprint 4
 * Cobre: sem usuário, sem permissions array, com permissions array.
 * Fallback por role removido: backend sempre retorna permissions[].
 */

import { renderHook } from "@testing-library/react"
import { usePermission } from "@/hooks/usePermission"
import { useAuth } from "@/contexts/AuthContext"
import type { AuthUser } from "@/types"

vi.mock("@/contexts/AuthContext")
const mockedUseAuth = vi.mocked(useAuth)

function makeUser(role: AuthUser["role"], permissions: string[] = []): AuthUser {
  return {
    id: "1",
    username: "test",
    role,
    is_active: true,
    permissions,
  }
}

describe("usePermission", () => {
  it("retorna false quando não há usuário", () => {
    mockedUseAuth.mockReturnValue({ user: null } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("mapping.write"))
    expect(result.current).toBe(false)
  })

  it("viewer sem permissions[] retorna false para qualquer perm", () => {
    mockedUseAuth.mockReturnValue({
      user: makeUser("viewer"),
    } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("mapping.write"))
    expect(result.current).toBe(false)
  })

  it("admin sem permissions[] retorna false (sem fallback por role)", () => {
    mockedUseAuth.mockReturnValue({
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("mapping.write"))
    expect(result.current).toBe(false)
  })

  it("engineer COM permissions=['mapping.write'] retorna true", () => {
    mockedUseAuth.mockReturnValue({
      user: makeUser("engineer", ["mapping.write", "mapping.read"]),
    } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("mapping.write"))
    expect(result.current).toBe(true)
  })

  it("viewer COM permissions=['mapping.read'] NÃO tem mapping.write", () => {
    mockedUseAuth.mockReturnValue({
      user: makeUser("viewer", ["mapping.read"]),
    } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("mapping.write"))
    expect(result.current).toBe(false)
  })

  it("admin COM permissions=['user.manage'] tem user.manage", () => {
    mockedUseAuth.mockReturnValue({
      user: makeUser("admin", ["user.manage", "mapping.write", "mapping.read"]),
    } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("user.manage"))
    expect(result.current).toBe(true)
  })

  it("viewer COM permissions=['user.manage'] não esperado — mas hook retorna true se perm estiver no array", () => {
    // O hook é agnóstico ao role; só verifica o array
    mockedUseAuth.mockReturnValue({
      user: makeUser("viewer", ["user.manage"]),
    } as ReturnType<typeof useAuth>)
    const { result } = renderHook(() => usePermission("user.manage"))
    expect(result.current).toBe(true)
  })
})
