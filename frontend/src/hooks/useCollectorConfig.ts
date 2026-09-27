"use client"

import { useCallback, useEffect, useState } from "react"
import * as api from "@/services/api"
// R2-8.8: `i18n.t()` direto (não o hook `useTranslation`) — mesmo padrão do
// `ErrorBoundary` (`src/components/shared/ErrorBoundary.tsx`): este é um hook
// de dados puro, sem componente próprio para "possuir" o `useTranslation`, e
// o valor só é lido dentro de um `catch` (não precisa re-renderizar ao trocar
// de idioma).
import i18n from "@/i18n"
import type {
  CollectorConfig,
  UpdateCollectorConfigRequest,
} from "@/types"

type Feedback =
  | {
      type: "success" | "error"
      message: string
    }
  | null

interface UseCollectorConfigReturn {
  config: CollectorConfig | null
  loading: boolean
  saving: boolean
  error: string | null
  feedback: Feedback
  saveConfig: (data: UpdateCollectorConfigRequest) => Promise<boolean>
  clearFeedback: () => void
  refetch: () => Promise<void>
}

export function useCollectorConfig(): UseCollectorConfigReturn {
  const [config, setConfig] = useState<CollectorConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Feedback>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const cfg = await api.getCollectorConfig()
      setConfig(cfg)
    } catch (err) {
      const msg = err instanceof Error ? err.message : i18n.t("config:collectorConfigHook.loadErrorFallback")
      setError(msg)
    } finally {
      setLoading(false)
    }
  }, [])

  const saveConfig = useCallback(
    async (data: UpdateCollectorConfigRequest): Promise<boolean> => {
      try {
        setSaving(true)
        setFeedback(null)
        const updated = await api.updateCollectorConfig(data)
        setConfig(updated)
        setFeedback({
          type: "success",
          message: i18n.t("config:collectorConfigHook.saveSuccess"),
        })
        return true
      } catch (err) {
        const msg = err instanceof Error ? err.message : i18n.t("config:collectorConfigHook.saveErrorFallback")
        setFeedback({ type: "error", message: msg })
        return false
      } finally {
        setSaving(false)
      }
    },
    [],
  )

  const clearFeedback = useCallback(() => setFeedback(null), [])

  useEffect(() => {
    void fetchData()
  }, [fetchData])

  return {
    config,
    loading,
    saving,
    error,
    feedback,
    saveConfig,
    clearFeedback,
    refetch: fetchData,
  }
}
