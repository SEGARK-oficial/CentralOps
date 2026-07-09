/**
 * useDiscoveredFields
 * Busca os campos JMESPath descobertos automaticamente para um mapping.
 * Retornado pelo endpoint GET /mappings/{id}/discover-fields.
 *
 * Backend retorna { fields: DiscoveredField[] } (rich shape com path,
 * occurrences, sample_values, first_seen_at). Para o consumer simples
 * (autocomplete), o hook expõe `fields: string[]` — apenas os paths,
 * já ordenados por occurrences DESC pelo backend. A versão rica fica
 * em `discovered` para casos que queiram mostrar contagem/samples na UI.
 *
 * Comportamento graceful: se mappingId for vazio, o endpoint retornar
 * payload inválido, ou falhar com qualquer erro, `fields` fica como []
 * — a UI cai para input texto livre sem travar.
 */

import { useEffect, useState } from "react"
import { getDiscoveredFields, type DiscoveredField } from "@/services/api"

interface UseDiscoveredFieldsReturn {
  /** Apenas os paths, ordem do backend (occurrences DESC). */
  fields: string[]
  /** Shape rico para UI que queira mostrar contagem/samples. */
  discovered: DiscoveredField[]
  isLoading: boolean
}

export function useDiscoveredFields(mappingId: string | undefined): UseDiscoveredFieldsReturn {
  const [discovered, setDiscovered] = useState<DiscoveredField[]>([])
  const [isLoading, setIsLoading] = useState(false)

  useEffect(() => {
    if (!mappingId) return

    const controller = new AbortController()
    setIsLoading(true)

    getDiscoveredFields(mappingId, { signal: controller.signal })
      .then((result) => {
        // Defensive: backend retorna { fields: [...] } mas tolera shape antigo
        const rawFields = Array.isArray(result?.fields) ? result.fields : []
        setDiscovered(rawFields)
      })
      .catch((e: unknown) => {
        // AbortError = desmount normal; qualquer outro erro = graceful fallback
        if (e instanceof Error && e.name === "AbortError") return
        setDiscovered([])
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
  }, [mappingId])

  const fields = discovered.map((f) => f.path)

  return { fields, discovered, isLoading }
}
