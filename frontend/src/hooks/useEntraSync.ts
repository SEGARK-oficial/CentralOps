"use client"

import { useCallback, useEffect, useState } from "react"
import * as api from "@/services/api"
import type { EntraSyncStatus, EntraSyncTriggerResult } from "@/types"

type SyncFeedback = { type: "success" | "error" | "info"; message: string } | null

interface UseEntraSyncReturn {
  syncStatus: EntraSyncStatus | null
  loadingStatus: boolean
  syncing: boolean
  feedback: SyncFeedback
  syncNow: () => Promise<EntraSyncTriggerResult | null>
  refreshStatus: () => Promise<void>
  clearFeedback: () => void
}

/**
 * Gerencia o painel de sincronização de usuários do Entra (Fase 2B).
 * Carrega o status inicial e expõe `syncNow` para disparar sync manual.
 * Após o sync, aguarda 2s e atualiza o status para refletir o resultado.
 */
export function useEntraSync(): UseEntraSyncReturn {
  const [syncStatus, setSyncStatus] = useState<EntraSyncStatus | null>(null)
  const [loadingStatus, setLoadingStatus] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [feedback, setFeedback] = useState<SyncFeedback>(null)

  const refreshStatus = useCallback(async () => {
    try {
      setLoadingStatus(true)
      const status = await api.getEntraSyncStatus()
      setSyncStatus(status)
    } catch (err) {
      // Falha silenciosa — o painel apenas não exibe dados anteriores
      setSyncStatus(null)
    } finally {
      setLoadingStatus(false)
    }
  }, [])

  const syncNow = useCallback(async (): Promise<EntraSyncTriggerResult | null> => {
    try {
      setSyncing(true)
      setFeedback(null)
      const result = await api.syncEntraNow()
      setFeedback({
        type: result.queued ? "success" : "info",
        message: result.message,
      })
      // Aguarda 2s e atualiza status para refletir o resultado do sync
      setTimeout(() => {
        void refreshStatus()
      }, 2000)
      return result
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao disparar sincronização"
      setFeedback({ type: "error", message: msg })
      return null
    } finally {
      setSyncing(false)
    }
  }, [refreshStatus])

  const clearFeedback = useCallback(() => setFeedback(null), [])

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  return {
    syncStatus,
    loadingStatus,
    syncing,
    feedback,
    syncNow,
    refreshStatus,
    clearFeedback,
  }
}
