"use client"

import { useState, useEffect, useCallback } from "react"
import { useTranslation } from "react-i18next"
import * as api from "@/services/api"
import type { DetectionRead, DetectionStatus } from "@/types"

// PERF-14: `listDetections` não devolve total nenhum (nem `X-Total-Count`,
// nem `{items,total}`) — é um array cru, capado em `limit`. Sem um total de
// verdade para "exibindo N de M", o sinal que SOBRA é a própria saturação do
// teto: se voltaram exatamente `limit` detecções, plausivelmente há mais que
// o backend não devolveu. `truncated` expõe esse sinal para a UI.
const DETECTIONS_LIMIT = 200

interface UseDetectionsReturn {
  detections: DetectionRead[]
  loading: boolean
  error: string | null
  statusFilter: DetectionStatus | ""
  setStatusFilter: (filter: DetectionStatus | "") => void
  refetch: () => Promise<void>
  triage: (id: number, status: DetectionStatus) => Promise<DetectionRead>
  /** `true` quando a resposta bateu no teto de `limit` — provável que existam
   *  mais detecções do que as exibidas (o backend não informa o total real). */
  truncated: boolean
}

export function useDetections(): UseDetectionsReturn {
  const { t } = useTranslation("detections")
  const [detections, setDetections] = useState<DetectionRead[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<DetectionStatus | "">("")

  const fetchDetections = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const data = await api.listDetections(
        statusFilter ? { status_filter: statusFilter, limit: DETECTIONS_LIMIT } : { limit: DETECTIONS_LIMIT },
      )
      setDetections(data)
    } catch (err) {
      // R2-5.4: fallback traduzido — antes ficava fixo em PT mesmo com o app
      // rodando em en/es (só entra quando `err` não é um `Error` de verdade,
      // ex.: uma rejeição com string crua).
      const errorMessage = err instanceof Error ? err.message : t("detections:list.feedback.loadError")
      setError(errorMessage)
    } finally {
      setLoading(false)
    }
  }, [statusFilter, t])

  const triage = useCallback(async (id: number, status: DetectionStatus): Promise<DetectionRead> => {
    const updated = await api.updateDetectionStatus(id, { status })
    setDetections((prev) =>
      prev.map((detection) => (detection.id === id ? updated : detection)),
    )
    return updated
  }, [])

  const refetch = useCallback(async () => {
    await fetchDetections()
  }, [fetchDetections])

  useEffect(() => {
    fetchDetections()
  }, [fetchDetections])

  return {
    detections,
    loading,
    error,
    statusFilter,
    setStatusFilter,
    refetch,
    triage,
    truncated: detections.length >= DETECTIONS_LIMIT,
  }
}
