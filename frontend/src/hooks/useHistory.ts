"use client"

import { useState, useCallback } from "react"
import { useAuth } from "@/contexts/AuthContext"
import * as api from "@/services/api"
import type { AuditFilters, AuditHistoryItem, HistoryItem, SearchHistoryItem } from "@/types"

interface UseHistoryReturn {
  operationHistory: HistoryItem[]
  auditHistory: AuditHistoryItem[]
  searchHistory: SearchHistoryItem[]
  loading: boolean
  error: string | null
  fetchHistory: (clientId?: number | null) => Promise<void>
  fetchAuditHistory: (filters?: AuditFilters) => Promise<void>
  downloadAuditCSV: (filters?: AuditFilters) => Promise<void>
  downloadCSV: (searchId: string) => Promise<void>
}

export function useHistory(): UseHistoryReturn {
  const { user } = useAuth()
  const [operationHistory, setOperationHistory] = useState<HistoryItem[]>([])
  const [auditHistory, setAuditHistory] = useState<AuditHistoryItem[]>([])
  const [searchHistory, setSearchHistory] = useState<SearchHistoryItem[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchHistory = useCallback(async (clientId?: number | null) => {
    try {
      setLoading(true)
      setError(null)

      const requests: [Promise<HistoryItem[]>, Promise<SearchHistoryItem[]>] = [
        api.listHistory(),
        api.listSearchHistory(clientId || undefined),
      ]

      const [operationsData, searchData] = await Promise.all(requests)

      setOperationHistory(operationsData)
      setSearchHistory(searchData)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao carregar histórico"
      setError(errorMessage)
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchAuditHistory = useCallback(
    async (filters?: AuditFilters) => {
      if (!user || user.role !== "admin") {
        setAuditHistory([])
        setError(null)
        return
      }

      try {
        setLoading(true)
        setError(null)
        const auditData = await api.listAuditHistoryFiltered(filters)
        setAuditHistory(auditData)
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : "Falha ao carregar auditoria"
        setError(errorMessage)
      } finally {
        setLoading(false)
      }
    },
    [user],
  )

  const downloadCSV = useCallback(async (searchId: string) => {
    try {
      await api.downloadStoredCSV(searchId)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao baixar CSV"
      throw new Error(errorMessage)
    }
  }, [])

  const downloadAuditCSV = useCallback(async (filters?: AuditFilters) => {
    if (!user || user.role !== "admin") {
      throw new Error("Acesso restrito a administradores")
    }

    try {
      await api.downloadAuditHistoryCSV(filters)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao exportar auditoria"
      throw new Error(errorMessage)
    }
  }, [user])

  return {
    operationHistory,
    auditHistory,
    searchHistory,
    loading,
    error,
    fetchHistory,
    fetchAuditHistory,
    downloadAuditCSV,
    downloadCSV,
  }
}
