/**
 * useBackfillJobs
 * Lista backfill jobs de uma integração, com polling e mutações create/cancel.
 *
 * - Polling a cada refreshIntervalMs (padrão 10s).
 * - Pausa polling quando document.hidden (page visibility API).
 * - Cancela polling no unmount.
 * - createJob e cancelJob fazem refetch após sucesso.
 *
 * PERF-07: `isLoading` só liga na 1ª carga e em troca de filtro/integração —
 * poll, refetch manual e o refetch pós create/cancel são SILENCIOSOS. Antes,
 * o tick de poll de 10s ligava `isLoading`, e `BackfillJobsTable` trocava a
 * tabela inteira por um spinner de página cheia a cada tick — perdendo scroll
 * e foco do usuário no meio de uma sessão de leitura.
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

  const refreshIntervalMs = options?.refreshIntervalMs ?? DEFAULT_REFRESH_MS
  const intervalRef = useRef<number | null>(null)

  // `showLoading=false` (poll, refetch manual, refetch pós mutação) atualiza
  // `items`/`total` sem tocar em `isLoading` — a tabela continua na tela,
  // com scroll e foco intactos, e só troca as linhas quando os dados chegam.
  const fetchJobs = useCallback(
    (showLoading: boolean): (() => void) => {
      if (!integrationId) return () => {}
      const controller = new AbortController()
      if (showLoading) setIsLoading(true)

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
          if (!controller.signal.aborted && showLoading) setIsLoading(false)
        })

      return () => controller.abort()
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [integrationId, filters?.status, filters?.limit, filters?.offset],
  )

  const refetch = useCallback(() => {
    fetchJobs(false)
  }, [fetchJobs])

  // Fetch principal: só ESTE efeito (integração/filtro mudou) mostra loading.
  useEffect(() => {
    return fetchJobs(true)
  }, [fetchJobs])

  // Polling com page visibility — sempre silencioso (ver PERF-07 acima).
  useEffect(() => {
    const startPolling = () => {
      if (intervalRef.current) window.clearInterval(intervalRef.current)
      intervalRef.current = window.setInterval(() => {
        if (!document.hidden) {
          fetchJobs(false)
        }
      }, refreshIntervalMs)
    }

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        // Retomou visibilidade: força refetch imediato e reinicia intervalo
        fetchJobs(false)
        startPolling()
      }
    }

    startPolling()
    document.addEventListener("visibilitychange", handleVisibilityChange)

    return () => {
      if (intervalRef.current) window.clearInterval(intervalRef.current)
      document.removeEventListener("visibilitychange", handleVisibilityChange)
    }
  }, [refreshIntervalMs, fetchJobs])

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
