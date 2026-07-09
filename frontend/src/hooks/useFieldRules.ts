/**
 * useFieldRules
 * Constrói um índice em memória: (vendor, event_type, field_path) → MatchedRule[]
 * a partir de todas as MappingDefinition ativas com suas versões correntes.
 *
 * Estratégia de fetch:
 * 1. GET /api/mappings → lista de definitions (sem rules).
 * 2. Para cada definition com current_version_id, GET /api/mappings/{id}/versions/{vid}
 *    → version com rules[].
 * 3. Extrai sources de: rule.source, rule.fallback_source[], rule.items[].source,
 *    preprocess[].source (exceto sources que começam com "_", pois são refs a campos
 *    virtuais de preprocess anterior, não campos raw).
 * 4. Normaliza path: remove JMESPath operators → dotted path comparável.
 * 5. Indexa por chave composta "vendor::event_type::normalized_source".
 *
 * Cache em módulo: fetches são reutilizados entre montagens na mesma sessão.
 * Reset via _resetFieldRulesCache() (somente para testes).
 */

import { useEffect, useState } from "react"
import type { MappingRule, MappingVersion, PreprocessOp } from "@/types"

// ── Tipos públicos ─────────────────────────────────────────────────────────────

export type MatchKind = "primary" | "fallback" | "array_builder_item" | "preprocess"

export interface MatchedRule {
  rule_target: string
  source: string            // JMESPath original (antes da normalização)
  match_kind: MatchKind
  mapping_definition_id: string
  vendor: string
  event_type: string
}

export interface FieldRulesIndex {
  lookup(vendor: string, event_type: string, field_path: string): MatchedRule[]
  count(vendor: string, event_type: string, field_path: string): number
}

export interface UseFieldRulesReturn {
  data: FieldRulesIndex | null
  loading: boolean
  error: Error | null
}

// ── Tipos internos de shape de API ────────────────────────────────────────────

interface MappingDefinitionItem {
  id: string
  vendor: string
  event_type: string
  current_version_id: string | null
}

// MappingVersion já carrega rules como MappingPayload (dict v2 com preprocess+rules).
type MappingVersionWithPreprocess = MappingVersion

// ── Cache em módulo ───────────────────────────────────────────────────────────

let _indexCache: FieldRulesIndex | null = null
let _inflight: Promise<FieldRulesIndex> | null = null

/** Exposto apenas para testes — reseta o cache entre casos. */
export function _resetFieldRulesCache(): void {
  _indexCache = null
  _inflight = null
}

// ── Normalização de path JMESPath ─────────────────────────────────────────────

/**
 * Normaliza um JMESPath para um dotted path comparável.
 * Exemplos:
 *   "endpoint[0].address"   → "endpoint.address"
 *   "items[*].value"        → "items.value"
 *   "data.nested"           → "data.nested"
 *   "_processed.x"          → "_processed.x"  (virtual; excluído antes de chamar)
 */
export function normalizeJmesPath(path: string): string {
  return path
    .replace(/\[\*\]/g, "")    // remove [*]
    .replace(/\[\d+\]/g, "")   // remove [N]
    .replace(/\.\./g, ".")      // colapsa .. em .
    .replace(/^\./, "")         // remove ponto inicial
    .replace(/\.$/, "")         // remove ponto final
}

// ── Lógica de matching ────────────────────────────────────────────────────────

/**
 * Retorna true se field_path é compatível com source_normalized.
 * Compatível = iguais, field_path é ancestral de source, ou source é ancestral de field_path.
 */
function pathMatches(fieldPath: string, sourceNorm: string): boolean {
  if (fieldPath === sourceNorm) return true
  if (fieldPath.startsWith(sourceNorm + ".")) return true
  if (sourceNorm.startsWith(fieldPath + ".")) return true
  return false
}

// ── Construção do índice ──────────────────────────────────────────────────────

type IndexMap = Map<string, MatchedRule[]>

function indexKey(vendor: string, event_type: string, normalizedSource: string): string {
  return `${vendor}::${event_type}::${normalizedSource}`
}

function addToIndex(
  map: IndexMap,
  vendor: string,
  event_type: string,
  source: string,
  matched: MatchedRule,
): void {
  // Sources que começam com "_" são refs a campos virtuais produzidos por preprocess;
  // não correspondem a nenhum field_path real de drift. Excluímos do índice.
  if (source.startsWith("_")) return

  const norm = normalizeJmesPath(source)
  if (!norm) return

  const key = indexKey(vendor, event_type, norm)
  const existing = map.get(key)
  if (existing) {
    existing.push(matched)
  } else {
    map.set(key, [matched])
  }
}

function buildIndexFromVersions(
  _definitions: MappingDefinitionItem[],
  versions: Array<{ definitionId: string; vendor: string; event_type: string; version: MappingVersionWithPreprocess }>,
): FieldRulesIndex {
  const map: IndexMap = new Map()

  for (const { definitionId, vendor, event_type, version } of versions) {
    // version.rules é o payload v2 (dict com preprocess+rules).
    const preprocess: PreprocessOp[] = version.rules?.preprocess ?? []
    for (const op of preprocess) {
      if (!op.source) continue
      addToIndex(map, vendor, event_type, op.source, {
        rule_target: op.target,
        source: op.source,
        match_kind: "preprocess",
        mapping_definition_id: definitionId,
        vendor,
        event_type,
      })
    }

    const rules: MappingRule[] = version.rules?.rules ?? []
    for (const rule of rules) {
      if (rule.kind === "array_builder") {
        // Array builder: sources estão nos items
        for (const item of rule.items ?? []) {
          if (!item.source) continue
          addToIndex(map, vendor, event_type, item.source, {
            rule_target: rule.target,
            source: item.source,
            match_kind: "array_builder_item",
            mapping_definition_id: definitionId,
            vendor,
            event_type,
          })
        }
      } else {
        // Scalar rule
        if (rule.source) {
          addToIndex(map, vendor, event_type, rule.source, {
            rule_target: rule.target,
            source: rule.source,
            match_kind: "primary",
            mapping_definition_id: definitionId,
            vendor,
            event_type,
          })
        }
        for (const fb of rule.fallback_source ?? []) {
          if (!fb) continue
          addToIndex(map, vendor, event_type, fb, {
            rule_target: rule.target,
            source: fb,
            match_kind: "fallback",
            mapping_definition_id: definitionId,
            vendor,
            event_type,
          })
        }
      }
    }
  }

  // Constrói o FieldRulesIndex com lookup O(n_sources) por chamada.
  // Como o número de sources distintos por (vendor, event_type) é geralmente
  // pequeno (~dezenas), a busca linear é aceitável. Para O(1) exato precisaríamos
  // de um índice invertido por prefixo, mas o payload de drift é mínimo.
  return {
    lookup(vendor: string, event_type: string, field_path: string): MatchedRule[] {
      const results: MatchedRule[] = []
      // Itera sobre todas as entradas do mapa para suportar both-direction matching
      for (const [key, rules] of map) {
        const [kv, ket, kpath] = key.split("::")
        if (kv !== vendor || ket !== event_type) continue
        if (pathMatches(field_path, kpath)) {
          results.push(...rules)
        }
      }
      return results
    },
    count(vendor: string, event_type: string, field_path: string): number {
      return this.lookup(vendor, event_type, field_path).length
    },
  }
}

// ── Fetch ─────────────────────────────────────────────────────────────────────

async function fetchAndBuildIndex(signal?: AbortSignal): Promise<FieldRulesIndex> {
  // 1. Busca lista de definitions
  const defsRes = await fetch("/api/mappings", {
    credentials: "include",
    signal,
  })
  if (!defsRes.ok) {
    throw new Error(`Falha ao buscar mappings: HTTP ${defsRes.status}`)
  }
  const defs: MappingDefinitionItem[] = await defsRes.json()

  // 2. Para cada definition com current_version_id, busca a versão corrente
  const fetchTasks = defs
    .filter((d) => d.current_version_id !== null && d.current_version_id !== undefined)
    .map(async (def) => {
      const url = `/api/mappings/${def.id}/versions/${def.current_version_id}`
      const vRes = await fetch(url, { credentials: "include", signal })
      if (!vRes.ok) return null
      const version: MappingVersionWithPreprocess = await vRes.json()
      return { definitionId: def.id, vendor: def.vendor, event_type: def.event_type, version }
    })

  const results = await Promise.all(fetchTasks)
  const versions = results.filter(Boolean) as Array<{
    definitionId: string
    vendor: string
    event_type: string
    version: MappingVersionWithPreprocess
  }>

  return buildIndexFromVersions(defs, versions)
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useFieldRules(): UseFieldRulesReturn {
  const [data, setData] = useState<FieldRulesIndex | null>(_indexCache)
  const [loading, setLoading] = useState(_indexCache === null)
  const [error, setError] = useState<Error | null>(null)

  useEffect(() => {
    // Cache hit síncrono
    if (_indexCache !== null) {
      setData(_indexCache)
      setLoading(false)
      return
    }

    const controller = new AbortController()

    if (_inflight === null) {
      _inflight = fetchAndBuildIndex(controller.signal)
        .then((index) => {
          _indexCache = index
          _inflight = null
          return index
        })
        .catch((e: unknown) => {
          _inflight = null
          throw e
        })
    }

    _inflight
      .then((index) => {
        if (!controller.signal.aborted) {
          setData(index)
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
