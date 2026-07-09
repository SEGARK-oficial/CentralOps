/**
 * useBackfillJobs
 * Lista backfill jobs de uma integração, com polling e mutações create/cancel.
 *
 * - Polling a cada refreshIntervalMs (padrão 10s).
 * - Pausa polling quando document.hidden (page visibility API).
 * - Cancela polling no unmount.
 * - createJob e cancelJob fazem refetch após sucesso.
 */

import { useCallback, useEffect, useRef, useState } from "react"
import type { BackfillJob, BackfillJobStatus, CreateBackfillJobRequest } from "@/types"
import { cancelBackfillJob, createBackfillJob, listBackfillJobs } from "@/services/api"

const DEFAULT_REFRESH_MS = 10_000

interface UseBackfillJobsFilters {
  status?: BackfillJobStatus
  limit?: number
  offset?: number
}

interface UseBackfillJobsOptions {
  refreshIntervalMs?: number
}

interface UseBackfillJobsReturn {
  items: BackfillJob[]
  total: number
  isLoading: boolean
  error: Error | null
  refetch: () => void
  createJob: (payload: CreateBackfillJobRequest) => Promise<BackfillJob>
  cancelJob: (jobId: string) => Promise<BackfillJob>
}

export function useBackfillJobs(
  integrationId: number,
  filters?: UseBackfillJobsFilters,
  options?: UseBackfillJobsOptions,
): UseBackfillJobsReturn {
  const [items, setItems] = useState<BackfillJob[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [tick, setTick] = useState(0)

  const refreshIntervalMs = options?.refreshIntervalMs ?? DEFAULT_REFRESH_MS
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const refetch = useCallback(() => setTick((t) => t + 1), [])

  // Fetch principal
  useEffect(() => {
    if (!integrationId) return

    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    listBackfillJobs(integrationId, filters, { signal: controller.signal })
      .then((res) => {
        setItems(res.items)
        setTotal(res.total)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [integrationId, tick, filters?.status, filters?.limit, filters?.offset])

  // Polling com page visibility
  useEffect(() => {
    const startPolling = () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
      intervalRef.current = setInterval(() => {
        if (!document.hidden) {
          setTick((t) => t + 1)
        }
      }, refreshIntervalMs)
    }

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        // Retomou visibilidade: força refetch imediato e reinicia intervalo
        setTick((t) => t + 1)
        startPolling()
      }
    }

    startPolling()
    document.addEventListener("visibilitychange", handleVisibilityChange)

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
      document.removeEventListener("visibilitychange", handleVisibilityChange)
    }
  }, [refreshIntervalMs])

  const createJob = useCallback(
    async (payload: CreateBackfillJobRequest): Promise<BackfillJob> => {
      const job = await createBackfillJob(integrationId, payload)
      refetch()
      return job
    },
    [integrationId, refetch],
  )

  const cancelJob = useCallback(
    async (jobId: string): Promise<BackfillJob> => {
      const job = await cancelBackfillJob(jobId)
      refetch()
      return job
    },
    [refetch],
  )

  return { items, total, isLoading, error, refetch, createJob, cancelJob }
}
