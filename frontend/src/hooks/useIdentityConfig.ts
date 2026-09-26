"use client"

import { useCallback, useEffect, useState } from "react"
import * as api from "@/services/api"
// R3-8.5: `i18n.t()` direto — ver comentário equivalente em useCollectorConfig.ts.
import i18n from "@/i18n"
import type {
  IdentityConfig,
  IdentityConnectionTestResult,
  UpdateIdentityConfigRequest,
} from "@/types"

type Feedback = { type: "success" | "error"; message: string } | null

interface UseIdentityConfigReturn {
  config: IdentityConfig | null
  loading: boolean
  saving: boolean
  testing: boolean
  testResult: IdentityConnectionTestResult | null
  error: string | null
  feedback: Feedback
  saveConfig: (data: UpdateIdentityConfigRequest) => Promise<boolean>
  testConnection: () => Promise<boolean>
  clearFeedback: () => void
  refetch: () => Promise<void>
}

export function useIdentityConfig(): UseIdentityConfigReturn {
  const [config, setConfig] = useState<IdentityConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<IdentityConnectionTestResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Feedback>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      setConfig(await api.getIdentityConfig())
    } catch (err) {
      setError(err instanceof Error ? err.message : i18n.t("config:identityConfigHook.loadError"))
    } finally {
      setLoading(false)
    }
  }, [])

  const saveConfig = useCallback(async (data: UpdateIdentityConfigRequest): Promise<boolean> => {
    try {
      setSaving(true)
      setFeedback(null)
      setConfig(await api.updateIdentityConfig(data))
      setFeedback({ type: "success", message: i18n.t("config:identityConfigHook.saveSuccess") })
      return true
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : i18n.t("config:identityConfigHook.saveError") })
      return false
    } finally {
      setSaving(false)
    }
  }, [])

  const testConnection = useCallback(async (): Promise<boolean> => {
    try {
      setTesting(true)
      setFeedback(null)
      const result = await api.testIdentityConnection()
      setTestResult(result)
      setFeedback({ type: result.ok ? "success" : "error", message: result.detail })
      return result.ok
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : i18n.t("config:identityConfigHook.testError") })
      return false
    } finally {
      setTesting(false)
    }
  }, [])

  const clearFeedback = useCallback(() => setFeedback(null), [])

  useEffect(() => {
    void fetchData()
  }, [fetchData])

  return {
    config,
    loading,
    saving,
    testing,
    testResult,
    error,
    feedback,
    saveConfig,
    testConnection,
    clearFeedback,
    refetch: fetchData,
  }
}
