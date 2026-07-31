/**
 * useMappingAudit
 * Busca entradas de auditoria para um mapping com paginação e filtros.
 */

import { useEffect, useState } from "react"
import type { MappingAuditEntry } from "@/types"
import { getMappingAudit } from "@/services/api"

interface AuditParams {
  limit?: number
  offset?: number
  action?: string
  username?: string
  from_ts?: string
  to_ts?: string
}

interface UseMappingAuditReturn {
  entries: MappingAuditEntry[]
  isLoading: boolean
  error: Error | null
  /**
   * Ações que o endpoint pode devolver, servidas pelo backend. Vazio enquanto
   * carrega ou se o backend for anterior ao campo — o consumidor decide o
   * fallback. Existe para o seletor de filtro não manter uma cópia própria da
   * lista, que já divergiu do backend em três valores.
   */
  availableActions: string[]
}

export function useMappingAudit(id: string, params?: AuditParams): UseMappingAuditReturn {
  const [entries, setEntries] = useState<MappingAuditEntry[]>([])
  const [availableActions, setAvailableActions] = useState<string[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)

  // Serializa params para usar como dep do useEffect
  const paramsKey = JSON.stringify(params)

  useEffect(() => {
    if (!id) return

    const controller = new AbortController()
    setIsLoading(true)
    setError(null)

    const resolvedParams: AuditParams = {
      limit: 50,
      offset: 0,
      ...params,
    }

    getMappingAudit(id, resolvedParams, { signal: controller.signal })
      .then((result) => {
        // Defensivo: garante array mesmo se o service retornar
        // undefined/null/formato antigo por mudança de contrato.
        setEntries(Array.isArray(result?.items) ? result.items : [])
        setAvailableActions(
          Array.isArray(result?.availableActions) ? result.availableActions : [],
        )
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
        setEntries([])
        // NÃO zera availableActions: o seletor some da tela no meio de um
        // erro de rede e o operador perde o filtro que estava usando.
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, paramsKey])

  return { entries, isLoading, error, availableActions }
}
