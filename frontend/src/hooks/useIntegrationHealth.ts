/**
 * useIntegrationHealth
 * Busca e atualiza automaticamente a saúde do pipeline de normalização para uma integração.
 * Pausa o polling quando a aba do navegador está oculta (Page Visibility API).
 * Cancela a requisição em curso ao desmontar.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import type { IntegrationPipelineHealth } from "@/types"
import { getIntegrationPipelineHealth } from "@/services/api"

interface UseIntegrationHealthOptions {
  refreshIntervalMs?: number
}

interface UseIntegrationHealthReturn {
  data: IntegrationPipelineHealth | null
  isLoading: boolean
  error: Error | null
  refetch: () => void
}

export function useIntegrationHealth(
  integrationId: number,
  options?: UseIntegrationHealthOptions,
): UseIntegrationHealthReturn {
  const refreshIntervalMs = options?.refreshIntervalMs ?? 60_000

  const [data, setData] = useState<IntegrationPipelineHealth | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [tick, setTick] = useState(0)
  const isMounted = useRef(true)

  const refetch = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    isMounted.current = true
    return () => {
      isMounted.current = false
    }
  }, [])

  // Fetch principal — dispara no mount e a cada refetch() manual.
  useEffect(() => {
    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    getIntegrationPipelineHealth(integrationId, { signal: controller.signal })
      .then((res) => {
        if (!controller.signal.aborted) {
          setData(res)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        if (!controller.signal.aborted) {
          setError(e instanceof Error ? e : new Error(String(e)))
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setIsLoading(false)
        }
      })

    return () => {
      controller.abort()
    }
  }, [integrationId, tick])

  // Auto-refresh via setInterval. Pausa quando página está oculta.
  useEffect(() => {
    const tick_ = () => {
      if (document.visibilityState !== "hidden") {
        setTick((t) => t + 1)
      }
    }

    const intervalId = setInterval(tick_, refreshIntervalMs)
    return () => clearInterval(intervalId)
  }, [refreshIntervalMs])

  return { data, isLoading, error, refetch }
}
