/**
 * useMapping
 * Busca a definição de um mapping e suas versões pelo ID.
 * Expõe refetch para forçar revalidação.
 */

import { useEffect, useState, useCallback } from "react"
import type { Mapping, MappingVersion } from "@/types"
import { getMapping } from "@/services/api"
import { ApiRequestError } from "@/services/api"

type MappingWithVersions = Mapping & { versions: MappingVersion[] }

interface UseMappingReturn {
  data: MappingWithVersions | null
  isLoading: boolean
  error: Error | null
  refetch: () => void
}

export function useMapping(id: string): UseMappingReturn {
  const [data, setData] = useState<MappingWithVersions | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [tick, setTick] = useState(0)

  const refetch = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    if (!id) return

    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    getMapping(id, { signal: controller.signal })
      .then((result) => {
        setData(result)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
        setData(null)
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
  }, [id, tick])

  return { data, isLoading, error, refetch }
}
