"use client"

import { useCallback, useEffect, useState } from "react"
import * as api from "@/services/api"
import type {
  CollectorConfig,
  CollectorConfigTestResponse,
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
  testing: boolean
  testResult: CollectorConfigTestResponse | null
  error: string | null
  feedback: Feedback
  saveConfig: (data: UpdateCollectorConfigRequest) => Promise<boolean>
  runTest: () => Promise<boolean>
  clearFeedback: () => void
  refetch: () => Promise<void>
}

export function useCollectorConfig(): UseCollectorConfigReturn {
  const [config, setConfig] = useState<CollectorConfig | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState<CollectorConfigTestResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<Feedback>(null)

  const fetchData = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const cfg = await api.getCollectorConfig()
      setConfig(cfg)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao carregar configuração"
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
          message: "Configuração salva. Workers refletirão em até 30s.",
        })
        return true
      } catch (err) {
        const msg = err instanceof Error ? err.message : "Falha ao salvar configuração"
        setFeedback({ type: "error", message: msg })
        return false
      } finally {
        setSaving(false)
      }
    },
    [],
  )

  const runTest = useCallback(async (): Promise<boolean> => {
    try {
      setTesting(true)
      setFeedback(null)
      const result = await api.testCollectorConfig()
      setTestResult(result)
      const anyError = result.results.some((r) => r.status === "error")
      setFeedback(
        anyError
          ? { type: "error", message: "Teste concluído com falhas — veja detalhes abaixo." }
          : { type: "success", message: "Conexão validada com sucesso." },
      )
      return !anyError
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Falha ao executar teste"
      setFeedback({ type: "error", message: msg })
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
    runTest,
    clearFeedback,
    refetch: fetchData,
  }
}
