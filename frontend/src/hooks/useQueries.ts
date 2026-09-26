"use client"

import { useState, useEffect, useCallback } from "react"
import * as api from "@/services/api"
// R3-8.5: `i18n.t()` direto — ver comentário equivalente em useCollectorConfig.ts.
import i18n from "@/i18n"
import type { Query } from "@/types"

interface UseQueriesReturn {
  queries: Query[]
  loading: boolean
  error: string | null
  createQuery: (data: Omit<Query, "id">) => Promise<Query>
  updateQuery: (id: number, data: Partial<Query>) => Promise<Query>
  deleteQuery: (id: number) => Promise<void>
  refetch: () => Promise<void>
}

export function useQueries(): UseQueriesReturn {
  const [queries, setQueries] = useState<Query[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchQueries = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const data = await api.listQueries()
      setQueries(data)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : i18n.t("queries:queriesHook.loadError")
      setError(errorMessage)
    } finally {
      setLoading(false)
    }
  }, [])

  const createQuery = useCallback(async (data: Omit<Query, "id">): Promise<Query> => {
    try {
      const newQuery = await api.createQuery(data)
      setQueries((prev) => [...prev, newQuery])
      return newQuery
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : i18n.t("queries:queriesHook.createError")
      throw new Error(errorMessage)
    }
  }, [])

  const updateQuery = useCallback(async (id: number, data: Partial<Query>): Promise<Query> => {
    try {
      const updatedQuery = await api.updateQuery(id, data)
      setQueries((prev) => prev.map((query) => (query.id === id ? updatedQuery : query)))
      return updatedQuery
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : i18n.t("queries:queriesHook.updateError")
      throw new Error(errorMessage)
    }
  }, [])

  const deleteQuery = useCallback(async (id: number): Promise<void> => {
    try {
      await api.deleteQuery(id)
      setQueries((prev) => prev.filter((query) => query.id !== id))
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : i18n.t("queries:queriesHook.deleteError")
      throw new Error(errorMessage)
    }
  }, [])

  const refetch = useCallback(async () => {
    await fetchQueries()
  }, [fetchQueries])

  useEffect(() => {
    fetchQueries()
  }, [fetchQueries])

  return {
    queries,
    loading,
    error,
    createQuery,
    updateQuery,
    deleteQuery,
    refetch,
  }
}
