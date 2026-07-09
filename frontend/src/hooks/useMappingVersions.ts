/**
 * useMappingVersions
 * Lista todas as versões de um mapping por ID.
 */

import { useEffect, useState } from "react"
import type { MappingVersion } from "@/types"
import { getMappingVersions } from "@/services/api"

interface UseMappingVersionsReturn {
  versions: MappingVersion[]
  isLoading: boolean
  error: Error | null
}

export function useMappingVersions(mappingId: string): UseMappingVersionsReturn {
  const [versions, setVersions] = useState<MappingVersion[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    if (!mappingId) return

    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    getMappingVersions(mappingId, { signal: controller.signal })
      .then((result) => {
        setVersions(result)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
        setVersions([])
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
  }, [mappingId])

  return { versions, isLoading, error }
}
