"use client"

import { useState, useCallback } from "react"
import { useTranslation } from "react-i18next"
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
  // R2-5.4: os fallbacks abaixo (usados só quando `err` não é um `Error` de
  // verdade) ficavam fixos em PT mesmo com o app rodando em en/es.
  const { t } = useTranslation("alerts")
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
      const errorMessage = err instanceof Error ? err.message : t("history.errors.loadHistoryFailed")
      setError(errorMessage)
    } finally {
      setLoading(false)
    }
  }, [t])

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
        const errorMessage = err instanceof Error ? err.message : t("history.errors.loadAuditFailed")
        setError(errorMessage)
      } finally {
        setLoading(false)
      }
    },
    [user, t],
  )

  const downloadCSV = useCallback(async (searchId: string) => {
    try {
      await api.downloadStoredCSV(searchId)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : t("history.errors.downloadCsvFailed")
      throw new Error(errorMessage)
    }
  }, [t])

  const downloadAuditCSV = useCallback(async (filters?: AuditFilters) => {
    if (!user || user.role !== "admin") {
      throw new Error(t("history.errors.adminOnly"))
    }

    try {
      await api.downloadAuditHistoryCSV(filters)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : t("history.errors.exportAuditFailed")
      throw new Error(errorMessage)
    }
  }, [user, t])

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
