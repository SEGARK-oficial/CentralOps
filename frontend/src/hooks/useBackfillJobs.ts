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
 *
 * R4-5.3 (regressão da R3-5.2): o "pula em vez de aborta" acima não distingue
 * POR QUE o tick é silencioso — um poll de rotina e um refetch pós-mutação
 * (`createJob`/`cancelJob`) ou o botão de atualizar do usuário caíam no MESMO
 * guard e eram descartados do MESMO jeito. Resultado: criar/cancelar um job
 * enquanto um poll de 10s calhava de estar em voo fazia o job novo só
 * aparecer no PRÓXIMO tick (até 10s depois), não na hora. Agora só o POLL de
 * rotina (`reason: "poll"`) descarta de verdade o tick; um `"refetch"`
 * (manual ou pós-mutação) que encontra algo em voo marca `pendingRefetchRef`
 * e é reexecutado no `finally` da request em voo, assim que ela terminar —
 * sem abortar nada (continua valendo a razão original do R3-5.2: abortar uma
 * carga inicial em andamento travava `isLoading`).
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
  // R4-5.3: um "refetch" (manual ou pós-mutação) que chegou enquanto algo já
  // estava em voo marca aqui — e é reexecutado no `finally` da request em
  // voo, assim que ela liberar. Só existe pra distinguir de um POLL de
  // rotina, que continua descartando o tick sem deixar rastro nenhum.
  const pendingRefetchRef = useRef(false)

  // `showLoading=false` (poll, refetch manual, refetch pós mutação) atualiza
  // `items`/`total` sem tocar em `isLoading` — a tabela continua na tela,
  // com scroll e foco intactos, e só troca as linhas quando os dados chegam.
  const fetchJobs = useCallback(
    (showLoading: boolean, reason: "load" | "poll" | "refetch"): (() => void) => {
      if (!integrationId) return () => {}
      if (inFlightRef.current && !showLoading) {
        if (reason === "poll") {
          // R3-5.2: um tick de POLL silencioso não pode abortar a request em
          // voo — se ela for a carga INICIAL (`showLoading=true`), abortá-la
          // aqui deixava o `isLoading` preso em `true` pra sempre (a geração
          // abortada não zerava, e esta, sendo silenciosa, também não). O
          // poll de rotina não precisa de "segunda tentativa": o próximo tick
          // já cobre o mesmo dado.
          return () => {}
        }
        // R4-5.3: um "refetch" (manual ou pós create/cancel) NÃO pode
        // simplesmente ser descartado como um tick de poll — o usuário
        // acabou de criar/cancelar um job e espera vê-lo JÁ, não no próximo
        // tick (até 10s depois). Também não pode ABORTAR a request em voo
        // (mesmo risco de skeleton eterno do R3-5.2, se essa request em voo
        // for a carga inicial) — só marca a intenção; `finally`, abaixo,
        // reexecuta assim que a request em voo liberar.
        pendingRefetchRef.current = true
        return () => {}
      }
      // showLoading=true (1ª carga, troca de integração/filtro): esses dados
      // JÁ NÃO INTERESSAM mais — pode abortar o que estiver em voo.
      activeControllerRef.current?.abort()
      const controller = new AbortController()
      activeControllerRef.current = controller
      inFlightRef.current = true
      // Esta request já vai buscar dado fresco — qualquer refetch pendente
      // de ANTES dela começar já está satisfeito de antemão.
      pendingRefetchRef.current = false
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
          // R4-5.3: um refetch pediu passagem enquanto esta request corria —
          // agora que ela liberou, roda de verdade (sem esperar o próximo
          // tick de poll).
          if (pendingRefetchRef.current) {
            pendingRefetchRef.current = false
            fetchJobs(false, "refetch")
          }
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
    fetchJobs(false, "refetch")
  }, [fetchJobs])

  // Fetch principal: só ESTE efeito (integração/filtro mudou) mostra loading.
  useEffect(() => {
    return fetchJobs(true, "load")
  }, [fetchJobs])

  // Polling com page visibility — sempre silencioso (ver PERF-07 acima).
  useEffect(() => {
    const startPolling = () => {
      if (intervalRef.current) window.clearInterval(intervalRef.current)
      intervalRef.current = window.setInterval(() => {
        if (!document.hidden) {
          fetchJobs(false, "poll")
        }
      }, refreshIntervalMs)
    }

    const handleVisibilityChange = () => {
      if (!document.hidden) {
        // Retomou visibilidade: força refetch imediato e reinicia intervalo
        fetchJobs(false, "poll")
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
