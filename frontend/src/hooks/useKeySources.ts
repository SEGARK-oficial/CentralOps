/**
 * useKeySources
 * Inventário de campos que a organização DE FATO produz, para os campos de
 * regra que resolvem sobre o envelope (`group_by_field`, `where.field` da
 * correlação em voo; `key.source` do enriquecimento já tem o seu).
 *
 * Retornado por GET /mappings/key-sources. O hook expõe `paths: string[]`
 * pronto para o `JMESPathInput` e `fromActiveMappings` para a UI dizer de
 * onde a sugestão veio — um catálogo estático apresentado como inventário
 * do cliente é pior que nenhuma sugestão.
 *
 * Comportamento graceful, igual ao `useDiscoveredFields`: qualquer falha
 * (endpoint ausente numa API mais velha, 422 por falta de org, rede) deixa
 * `paths` vazio e a UI cai para texto livre sem travar. `roots` tem um
 * fallback fixo porque a validação de prefixo não pode depender da rede.
 */

import { useEffect, useState } from "react"
import { listMappingKeySources, type MappingKeySource } from "@/services/api"

/** Raízes do envelope — espelha `ENVELOPE_ROOTS` do backend. Usado só como fallback. */
export const ENVELOPE_ROOTS_FALLBACK: readonly string[] = ["_centralops", "normalized", "raw"]

interface UseKeySourcesReturn {
  /** Só os caminhos, na ordem do backend (mapeados → catálogo → envelope). */
  paths: string[]
  /** Shape rico, para UI que queira mostrar vendors/contagem. */
  sources: MappingKeySource[]
  /** `true` quando a lista reflete os mappings ATIVOS da org, não o catálogo. */
  fromActiveMappings: boolean
  /** Raízes válidas para um path sobre o envelope. */
  roots: readonly string[]
  isLoading: boolean
}

export function useKeySources(organizationId?: number | null, enabled = true): UseKeySourcesReturn {
  const [sources, setSources] = useState<MappingKeySource[]>([])
  const [fromActiveMappings, setFromActiveMappings] = useState(false)
  const [roots, setRoots] = useState<readonly string[]>(ENVELOPE_ROOTS_FALLBACK)
  const [isLoading, setIsLoading] = useState(false)

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    setIsLoading(true)

    listMappingKeySources(
      organizationId != null ? { organization_id: organizationId } : {},
      { signal: controller.signal },
    )
      .then((result) => {
        setSources(Array.isArray(result?.suggestions) ? result.suggestions : [])
        setFromActiveMappings(Boolean(result?.from_active_mappings))
        if (Array.isArray(result?.roots) && result.roots.length > 0) setRoots(result.roots)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setSources([])
        setFromActiveMappings(false)
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })

    return () => controller.abort()
  }, [organizationId, enabled])

  return {
    paths: sources.map((s) => s.path),
    sources,
    fromActiveMappings,
    roots,
    isLoading,
  }
}

/**
 * Um path resolve sobre o envelope? Primeiro segmento em `roots`. É a mesma
 * regra que o backend aplica (`inflight/runtime.py`, razão `group_by_root`);
 * aqui ela vira erro de formulário em vez de 422 — ou, pior, de regra verde
 * que nunca dispara.
 */
export function hasEnvelopeRoot(path: string, roots: readonly string[] = ENVELOPE_ROOTS_FALLBACK): boolean {
  const first = path.trim().split(".", 1)[0]
  return roots.includes(first)
}
