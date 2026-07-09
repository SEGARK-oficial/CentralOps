"use client"

import { useState, useEffect, useCallback } from "react"
import * as api from "@/services/api"
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
      const errorMessage = err instanceof Error ? err.message : "Falha ao carregar queries"
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
      const errorMessage = err instanceof Error ? err.message : "Falha ao criar query"
      throw new Error(errorMessage)
    }
  }, [])

  const updateQuery = useCallback(async (id: number, data: Partial<Query>): Promise<Query> => {
    try {
      const updatedQuery = await api.updateQuery(id, data)
      setQueries((prev) => prev.map((query) => (query.id === id ? updatedQuery : query)))
      return updatedQuery
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao atualizar query"
      throw new Error(errorMessage)
    }
  }, [])

  const deleteQuery = useCallback(async (id: number): Promise<void> => {
    try {
      await api.deleteQuery(id)
      setQueries((prev) => prev.filter((query) => query.id !== id))
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao deletar query"
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
