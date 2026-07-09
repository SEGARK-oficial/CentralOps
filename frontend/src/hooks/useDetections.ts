"use client"

import { useState, useEffect, useCallback } from "react"
import * as api from "@/services/api"
import type { DetectionRead, DetectionStatus } from "@/types"

interface UseDetectionsReturn {
  detections: DetectionRead[]
  loading: boolean
  error: string | null
  statusFilter: DetectionStatus | ""
  setStatusFilter: (filter: DetectionStatus | "") => void
  refetch: () => Promise<void>
  triage: (id: number, status: DetectionStatus) => Promise<DetectionRead>
}

export function useDetections(): UseDetectionsReturn {
  const [detections, setDetections] = useState<DetectionRead[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState<DetectionStatus | "">("")

  const fetchDetections = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const data = await api.listDetections(
        statusFilter ? { status_filter: statusFilter, limit: 200 } : { limit: 200 },
      )
      setDetections(data)
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao carregar detecções"
      setError(errorMessage)
    } finally {
      setLoading(false)
    }
  }, [statusFilter])

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
  }
}
