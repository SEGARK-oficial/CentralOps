"use client"

import { useState, useEffect, useCallback, useRef } from "react"
import * as api from "@/services/api"
import type { Client, Integration } from "@/types"

interface UseClientsReturn {
  clients: Client[]
  loading: boolean
  error: string | null
  refetch: () => Promise<void>
}

function integrationToClient(integration: Integration): Client {
  return {
    id: integration.id,
    name: integration.name,
    region: integration.region,
    client_id: integration.client_id,
    tenant_id: integration.tenant_id,
    is_authenticated: integration.is_authenticated,
  }
}

function normalizeIntegrationsPayload(payload: unknown): Integration[] {
  if (Array.isArray(payload)) return payload as Integration[]
  if (
    payload &&
    typeof payload === "object" &&
    "items" in payload &&
    Array.isArray((payload as { items?: unknown[] }).items)
  ) {
    return (payload as { items: Integration[] }).items
  }
  return []
}

export function useClients(): UseClientsReturn {
  const [clients, setClients] = useState<Client[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const hasFetchedRef = useRef(false)

  const fetchClients = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)

      const integrations = normalizeIntegrationsPayload(
        await api.listIntegrations(undefined, "sophos"),
      )
      setClients(integrations.map(integrationToClient))
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : "Falha ao carregar clientes"
      setError(errorMessage)
    } finally {
      setLoading(false)
    }
  }, [])

  const refetch = useCallback(async () => {
    await fetchClients()
  }, [fetchClients])

  useEffect(() => {
    if (!hasFetchedRef.current) {
      hasFetchedRef.current = true
      fetchClients()
    }
  }, [])

  return {
    clients,
    loading,
    error,
    refetch,
  }
}
