"use client"

import { useCallback, useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { McpConfig, UpdateMcpConfigRequest } from "@/types"

type Feedback = { type: "success" | "error"; message: string } | null

interface UseMcpConfigReturn {
  config: McpConfig | null
  loading: boolean
  saving: boolean
  error: string | null
  feedback: Feedback
  saveConfig: (data: UpdateMcpConfigRequest) => Promise<boolean>
  clearFeedback: () => void
  refetch: () => Promise<void>
}

export function useMcpConfig(): UseMcpConfigReturn {
  const { t } = useTranslation("config")
  const [config, setConfig] = useState<McpConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Feedback>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setConfig(await api.getMcpConfig())
    } catch (err) {
      setError(err instanceof Error ? err.message : t("page.mcp.loadError"))
    } finally {
      setLoading(false)
    }
  }, [t])

  const saveConfig = useCallback(async (data: UpdateMcpConfigRequest): Promise<boolean> => {
    try {
      setSaving(true)
      setFeedback(null)
      setConfig(await api.updateMcpConfig(data))
      setFeedback({ type: "success", message: t("mcp.saved") })
      return true
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("mcp.saveError") })
      return false
    } finally {
      setSaving(false)
    }
  }, [t])

  const clearFeedback = useCallback(() => setFeedback(null), [])

  useEffect(() => {
    void fetchData()
  }, [fetchData])

  return { config, loading, saving, error, feedback, saveConfig, clearFeedback, refetch: fetchData }
}
