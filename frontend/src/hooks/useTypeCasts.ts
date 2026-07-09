/**
 * useTypeCasts
 * Busca a lista dinâmica de funções de cast disponíveis no backend.
 * Endpoint: GET /api/mappings/normalize/type-casts
 *
 * Comportamento:
 * - Fetch único no mount; sem polling, sem refetch on focus.
 * - Cache em módulo: montagens subsequentes recebem o valor em cache
 *   de forma síncrona, sem waterfall.
 * - AbortController no unmount para evitar setState em componente
 *   desmontado.
 * - Loading visível apenas no PRIMEIRO fetch (enquanto cache vazio).
 */

import { useEffect, useState } from "react"
import { fetchTypeCasts } from "@/services/api"
import type { TypeCastDescriptor } from "@/types"

// ── Cache em módulo ───────────────────────────────────────────────────────────
// Persistente entre montagens dentro da mesma sessão de SPA.
// Não persiste entre page reloads (intencional: dados são leves e o registry
// pode mudar com deploys).

let _cache: TypeCastDescriptor[] | null = null
let _inflight: Promise<TypeCastDescriptor[]> | null = null

// Exposto apenas para testes — reseta o cache entre casos.
export function _resetTypeCastsCache() {
  _cache = null
  _inflight = null
}

// ── Return type ───────────────────────────────────────────────────────────────

export interface UseTypeCastsReturn {
  data: TypeCastDescriptor[] | null
  loading: boolean
  error: Error | null
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useTypeCasts(): UseTypeCastsReturn {
  // Se já temos cache, inicia com os dados sem loading.
  const [data, setData] = useState<TypeCastDescriptor[] | null>(_cache)
  const [loading, setLoading] = useState(_cache === null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    // Cache hit síncrono: nada a fazer.
    if (_cache !== null) {
      setData(_cache)
      setLoading(false)
      return
    }

    const controller = new AbortController()

    // Reutiliza inflight se outra instância do hook já disparou o fetch.
    if (_inflight === null) {
      _inflight = fetchTypeCasts({ signal: controller.signal })
        .then((result) => {
          _cache = result
          _inflight = null
          return result
        })
        .catch((e: unknown) => {
          _inflight = null
          throw e
        })
    }

    _inflight
      .then((result) => {
        if (!controller.signal.aborted) {
          setData(result)
          setError(null)
        }
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })

    return () => controller.abort()
  }, [])

  return { data, loading, error }
}
