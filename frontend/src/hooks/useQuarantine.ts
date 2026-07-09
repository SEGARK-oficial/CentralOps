/**
 * useQuarantine
 * Busca entradas de quarentena com filtros paginados.
 * Expõe mutations: discard, reprocess e getter: getDetail.
 */

import { useCallback, useEffect, useState } from "react"
import type { QuarantineDetail, QuarantineEntry } from "@/types"
import {
  discardQuarantine,
  getQuarantineDetail,
  listQuarantine,
  reprocessQuarantine,
} from "@/services/api"

export interface QuarantineFilters {
  vendor?: string
  event_type?: string
  error_kind?: string
  integration_id?: number
  /** PR #3: substring case-insensitive sobre Integration.name. */
  integration_name?: string
  /** PR #3: filtra por reprocessed_at. Default no backend = "pending". */
  status?: "pending" | "reprocessed" | "all"
  limit?: number
  offset?: number
}

interface UseQuarantineReturn {
  items: QuarantineEntry[]
  total: number
  isLoading: boolean
  error: Error | null
  refetch: () => void
  discard: (id: string) => Promise<void>
  reprocess: (id: string) => Promise<QuarantineEntry>
  getDetail: (id: string) => Promise<QuarantineDetail>
}

export function useQuarantine(filters: QuarantineFilters): UseQuarantineReturn {
  const [items, setItems] = useState<QuarantineEntry[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [tick, setTick] = useState(0)

  const refetch = useCallback(() => setTick((t) => t + 1), [])

  const filtersKey = JSON.stringify(filters)

  useEffect(() => {
    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    listQuarantine(filters, { signal: controller.signal })
      .then((res) => {
        setItems(res.items)
        setTotal(res.total)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
        setItems([])
        setTotal(0)
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey, tick])

  const discard = useCallback(
    async (id: string) => {
      await discardQuarantine(id)
      refetch()
    },
    [refetch],
  )

  const reprocess = useCallback(
    async (id: string): Promise<QuarantineEntry> => {
      const updated = await reprocessQuarantine(id)
      refetch()
      return updated
    },
    [refetch],
  )

  const getDetail = useCallback(
    (id: string) => getQuarantineDetail(id),
    [],
  )

  return { items, total, isLoading, error, refetch, discard, reprocess, getDetail }
}
