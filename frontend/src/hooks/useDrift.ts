/**
 * useDrift
 * Busca entradas de drift com filtros paginados.
 * Expõe mutations: ignoreField, markMapped, deleteField — todas com refetch automático.
 */

import { useCallback, useEffect, useState } from "react"
import type { DriftEntry } from "@/types"
import {
  bulkIgnoreDrift,
  bulkMarkDriftMapped,
  deleteDrift,
  ignoreDrift,
  listDrift,
  markDriftMapped,
} from "@/services/api"

export interface DriftFilters {
  vendor?: string
  event_type?: string
  status?: "new" | "ignored" | "mapped"
  limit?: number
  offset?: number
}

interface UseDriftReturn {
  items: DriftEntry[]
  total: number
  isLoading: boolean
  error: Error | null
  refetch: () => void
  ignoreField: (id: string) => Promise<void>
  markMapped: (id: string) => Promise<void>
  deleteField: (id: string) => Promise<void>
  bulkIgnore: (ids: string[]) => Promise<void>
  bulkMarkMapped: (ids: string[]) => Promise<void>
}

export function useDrift(filters: DriftFilters): UseDriftReturn {
  const [items, setItems] = useState<DriftEntry[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [tick, setTick] = useState(0)

  const refetch = useCallback(() => setTick((t) => t + 1), [])

  // Serialise filters to detect changes via useEffect dep
  const filtersKey = JSON.stringify(filters)

  useEffect(() => {
    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    listDrift(filters, { signal: controller.signal })
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

  const ignoreField = useCallback(
    async (id: string) => {
      await ignoreDrift(id)
      refetch()
    },
    [refetch],
  )

  const markMapped = useCallback(
    async (id: string) => {
      await markDriftMapped(id)
      refetch()
    },
    [refetch],
  )

  const deleteField = useCallback(
    async (id: string) => {
      await deleteDrift(id)
      refetch()
    },
    [refetch],
  )

  const bulkIgnore = useCallback(
    async (ids: string[]) => {
      await bulkIgnoreDrift(ids)
      refetch()
    },
    [refetch],
  )

  const bulkMarkMapped = useCallback(
    async (ids: string[]) => {
      await bulkMarkDriftMapped(ids)
      refetch()
    },
    [refetch],
  )

  return { items, total, isLoading, error, refetch, ignoreField, markMapped, deleteField, bulkIgnore, bulkMarkMapped }
}
