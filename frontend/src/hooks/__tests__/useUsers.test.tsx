import { renderHook, act, waitFor } from "@testing-library/react"
import { useUsers } from "@/hooks/useUsers"
import * as api from "@/services/api"
import type { AppUser } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const fakeUser: AppUser = {
  id: "1",
  username: "alice",
  display_name: "Alice",
  role: "engineer",
  is_active: true,
  permissions: ["mapping.write"],
  organization_id: null,
  organization_name: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
}

describe("useUsers", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockedApi.listUsers.mockResolvedValue([fakeUser])
    mockedApi.createUser.mockResolvedValue({ ...fakeUser, id: "2", username: "bob" })
    mockedApi.updateUser.mockResolvedValue({ ...fakeUser, role: "admin" })
    mockedApi.deleteUser.mockResolvedValue(undefined)
  })

  it("carrega lista de usuários na montagem", async () => {
    const { result } = renderHook(() => useUsers())
    expect(result.current.isLoading).toBe(true)
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.users).toHaveLength(1)
    expect(result.current.users[0].username).toBe("alice")
    expect(result.current.error).toBeNull()
  })

  it("expõe erro quando listUsers falha", async () => {
    mockedApi.listUsers.mockRejectedValue(new Error("Erro de rede"))
    const { result } = renderHook(() => useUsers())
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.error).not.toBeNull()
    expect(result.current.error?.message).toBe("Erro de rede")
  })

  it("createUser chama api.createUser e faz refetch", async () => {
    const { result } = renderHook(() => useUsers())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    mockedApi.listUsers.mockResolvedValue([fakeUser, { ...fakeUser, id: "2", username: "bob" }])
    await act(async () => {
      await result.current.createUser({
        username: "bob",
        password: "secret1234",
        role: "viewer",
      })
    })

    expect(mockedApi.createUser).toHaveBeenCalledWith({ username: "bob", password: "secret1234", role: "viewer" })
    expect(result.current.users).toHaveLength(2)
  })

  it("updateUser chama api.updateUser e faz refetch", async () => {
    const { result } = renderHook(() => useUsers())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    mockedApi.listUsers.mockResolvedValue([{ ...fakeUser, role: "admin" }])
    await act(async () => {
      await result.current.updateUser("1", { role: "admin" })
    })

    expect(mockedApi.updateUser).toHaveBeenCalledWith("1", { role: "admin" })
    expect(result.current.users[0].role).toBe("admin")
  })

  it("deleteUser chama api.deleteUser e faz refetch", async () => {
    const { result } = renderHook(() => useUsers())
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    mockedApi.listUsers.mockResolvedValue([])
    await act(async () => {
      await result.current.deleteUser("1")
    })

    expect(mockedApi.deleteUser).toHaveBeenCalledWith("1")
    expect(result.current.users).toHaveLength(0)
  })

  it("refetch recarrega lista", async () => {
    const { result } = renderHook(() => useUsers())
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(mockedApi.listUsers).toHaveBeenCalledTimes(1)

    act(() => {
      void result.current.refetch()
    })
    await waitFor(() => expect(mockedApi.listUsers).toHaveBeenCalledTimes(2))
  })
})
