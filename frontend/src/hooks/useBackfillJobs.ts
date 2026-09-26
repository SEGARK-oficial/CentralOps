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
 *
 * R3-5.2 (regressão da R2-5.4): o guard de aborto que evita corrida de
 * respostas (`activeControllerRef`) abortava QUALQUER fetch em voo, inclusive
 * a carga INICIAL (`showLoading=true`) quando o poll de 10s ou um
 * `visibilitychange` caíam no meio dela. A geração abortada não zerava
 * `isLoading` (`controller.signal.aborted` bloqueava o `finally`), e a nova
 * geração, sendo silenciosa, também não — skeleton eterno. Agora: um poll/
 * refetch silencioso (`showLoading=false`) que encontra uma request em voo
 * PULA o tick em vez de abortar; só uma chamada com `showLoading=true` (1ª
 * carga, troca de integração/filtro) pode abortar a geração anterior — e
 * QUALQUER geração que termine sem ter sido abortada zera `isLoading`,
 * mesmo que ela própria fosse silenciosa.
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
  // R2-5.4 + R3-5.2: só o request MAIS RECENTE importa — mas só uma chamada
  // com `showLoading=true` pode ABORTAR a geração anterior (ver docstring do
  // topo). `inFlightRef` é o que deixa um tick silencioso (poll/visibility/
  // refetch pós mutação) PULAR em vez de abortar quando já há algo em voo.
  const activeControllerRef = useRef<AbortController | null>(null)
  const inFlightRef = useRef(false)

  // `showLoading=false` (poll, refetch manual, refetch pós mutação) atualiza
  // `items`/`total` sem tocar em `isLoading` — a tabela continua na tela,
  // com scroll e foco intactos, e só troca as linhas quando os dados chegam.
  const fetchJobs = useCallback(
    (showLoading: boolean): (() => void) => {
      if (!integrationId) return () => {}
      if (inFlightRef.current && !showLoading) {
        // R3-5.2: um tick silencioso não pode abortar a request em voo — se
        // ela for a carga INICIAL (`showLoading=true`), abortá-la aqui
        // deixava o `isLoading` preso em `true` pra sempre (a geração
        // abortada não zerava, e esta, sendo silenciosa, também não).
        return () => {}
      }
      // showLoading=true (1ª carga, troca de integração/filtro): esses dados
      // JÁ NÃO INTERESSAM mais — pode abortar o que estiver em voo.
      activeControllerRef.current?.abort()
      const controller = new AbortController()
      activeControllerRef.current = controller
      inFlightRef.current = true
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
          // Resposta de uma geração VELHA (superada por uma troca de
          // filtro/integração) — nada a fazer, quem zera o loading é a
          // geração corrente.
          if (controller.signal.aborted) return
          inFlightRef.current = false
          activeControllerRef.current = null
          // R3-5.2: zera sempre que a geração CORRENTE termina, mesmo se ELA
          // MESMA era silenciosa — é o único jeito de garantir que uma carga
          // inicial lenta (que um poll só pulou, nunca abortou) sempre acabe
          // limpando o skeleton.
          setIsLoading(false)
        })

      return () => controller.abort()
    },
    // Deps NARROW (status/limit/offset), não o objeto `filters` inteiro — o
    // caller comum passa um literal `{ status, limit, offset }` inline, um
    // objeto NOVO a cada render com os MESMOS valores; depender do objeto
    // recriaria `fetchJobs` (e reabortaria a request em voo) sem motivo.
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
