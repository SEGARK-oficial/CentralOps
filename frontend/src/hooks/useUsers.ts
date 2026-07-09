import { useCallback, useEffect, useState } from "react"
import * as api from "@/services/api"
import type { AppUser, CreateUserRequest, UpdateUserRequest } from "@/types"

interface UseUsersReturn {
  users: AppUser[]
  isLoading: boolean
  error: Error | null
  refetch: () => void
  createUser: (payload: CreateUserRequest) => Promise<AppUser>
  updateUser: (id: string, payload: UpdateUserRequest) => Promise<AppUser>
  deleteUser: (id: string) => Promise<void>
}

export function useUsers(): UseUsersReturn {
  const [users, setUsers] = useState<AppUser[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  const fetchUsers = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const data = await api.listUsers()
      setUsers(data)
    } catch (e) {
      setError(e instanceof Error ? e : new Error("Falha ao carregar usuários"))
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchUsers()
  }, [fetchUsers])

  const createUser = useCallback(async (payload: CreateUserRequest): Promise<AppUser> => {
    const created = await api.createUser(payload)
    await fetchUsers()
    return created
  }, [fetchUsers])

  const updateUser = useCallback(async (id: string, payload: UpdateUserRequest): Promise<AppUser> => {
    const updated = await api.updateUser(id, payload)
    await fetchUsers()
    return updated
  }, [fetchUsers])

  const deleteUser = useCallback(async (id: string): Promise<void> => {
    await api.deleteUser(id)
    await fetchUsers()
  }, [fetchUsers])

  return {
    users,
    isLoading,
    error,
    refetch: fetchUsers,
    createUser,
    updateUser,
    deleteUser,
  }
}
