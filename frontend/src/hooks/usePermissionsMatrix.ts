import { useEffect, useState } from "react"
import * as api from "@/services/api"
import type { UserRole } from "@/types"

type PermissionsMatrix = Record<UserRole, string[]>

// Cache em memória — evita refetch entre componentes na mesma sessão
let cachedMatrix: PermissionsMatrix | null = null

interface UsePermissionsMatrixReturn {
  matrix: PermissionsMatrix | null
  isLoading: boolean
  error: Error | null
}

export function usePermissionsMatrix(): UsePermissionsMatrixReturn {
  const [matrix, setMatrix] = useState<PermissionsMatrix | null>(cachedMatrix)
  const [isLoading, setIsLoading] = useState(cachedMatrix === null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (cachedMatrix !== null) {
      setMatrix(cachedMatrix)
      setIsLoading(false)
      return
    }

    let cancelled = false
    setIsLoading(true)
    setError(null)

    api
      .getPermissionsMatrix()
      .then((data) => {
        if (!cancelled) {
          cachedMatrix = data as PermissionsMatrix
          setMatrix(cachedMatrix)
        }
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e instanceof Error ? e : new Error("Falha ao carregar matriz de permissões"))
        }
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  return { matrix, isLoading, error }
}

/** Limpa o cache (útil em testes) */
export function clearPermissionsMatrixCache(): void {
  cachedMatrix = null
}
