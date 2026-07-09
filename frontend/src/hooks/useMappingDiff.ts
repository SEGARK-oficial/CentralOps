/**
 * useMappingDiff
 * Busca o diff entre duas versões de um mapping pelo ID.
 * Desativado quando a ou b for null.
 */

import { useEffect, useState } from "react"
import type { MappingVersionDiffResponse } from "@/services/api"
import { getMappingDiff } from "@/services/api"

interface UseMappingDiffReturn {
  diff: MappingVersionDiffResponse | null
  isLoading: boolean
  error: Error | null
}

export function useMappingDiff(
  mappingId: string,
  a: string | null,
  b: string | null,
): UseMappingDiffReturn {
  const [diff, setDiff] = useState<MappingVersionDiffResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (!mappingId || a === null || b === null) {
      setDiff(null)
      setIsLoading(false)
      setError(null)
      return
    }

    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    getMappingDiff(mappingId, a, b, { signal: controller.signal })
      .then((result) => {
        setDiff(result)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
        setDiff(null)
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
  }, [mappingId, a, b])

  return { diff, isLoading, error }
}
