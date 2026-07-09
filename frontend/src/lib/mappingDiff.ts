/**
 * mappingDiff — computação client-side de diff entre versões de MappingRule[].
 * Produz a mesma shape que o endpoint GET /api/mappings/{id}/versions/{a}/diff/{b}.
 */

import type { MappingRule } from "@/types"

export type RuleSnapshot = MappingRule

export interface ModifiedRule {
  target: string
  before: RuleSnapshot
  after: RuleSnapshot
}

export interface MappingVersionDiff {
  reordered_only: boolean
  added: RuleSnapshot[]
  removed: RuleSnapshot[]
  modified: ModifiedRule[]
}

/**
 * deepEqual — igualdade profunda tolerante a undefined/null.
 * Trata undefined e null como distintos para campos de regra (source, const, etc.).
 */
function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (a === null || b === null) return a === b
  if (a === undefined || b === undefined) return a === b
  if (typeof a !== typeof b) return false
  if (typeof a !== "object") return a === b

  const aObj = a as Record<string, unknown>
  const bObj = b as Record<string, unknown>

  const aKeys = Object.keys(aObj)
  const bKeys = Object.keys(bObj)

  if (aKeys.length !== bKeys.length) return false

  for (const key of aKeys) {
    if (!Object.prototype.hasOwnProperty.call(bObj, key)) return false
    if (!deepEqual(aObj[key], bObj[key])) return false
  }

  return true
}

/**
 * rulesEqual — compara duas regras por todas as chaves relevantes.
 */
function rulesEqual(a: MappingRule, b: MappingRule): boolean {
  return (
    a.target === b.target &&
    deepEqual(a.source ?? null, b.source ?? null) &&
    deepEqual(a.const, b.const) &&
    deepEqual(a.default, b.default) &&
    deepEqual(a.value_map ?? null, b.value_map ?? null) &&
    (a.type_cast ?? null) === (b.type_cast ?? null) &&
    (a.required ?? false) === (b.required ?? false)
  )
}

/**
 * computeDiff — calcula diferenças entre versão a (antes) e b (depois).
 *
 * Comportamento:
 * - added: regras em b cujo target não existe em a.
 * - removed: regras em a cujo target não existe em b.
 * - modified: regras em ambos com payload diferente (por target).
 * - reordered_only: mesmos targets, modified vazio, mas ordem difere.
 */
export function computeDiff(a: MappingRule[], b: MappingRule[]): MappingVersionDiff {
  const aByTarget = new Map<string, MappingRule>(a.map((r) => [r.target, r]))
  const bByTarget = new Map<string, MappingRule>(b.map((r) => [r.target, r]))

  const added: RuleSnapshot[] = []
  const removed: RuleSnapshot[] = []
  const modified: ModifiedRule[] = []

  // Detectar adicionadas e modificadas
  for (const [target, bRule] of bByTarget) {
    const aRule = aByTarget.get(target)
    if (!aRule) {
      added.push(bRule)
    } else if (!rulesEqual(aRule, bRule)) {
      modified.push({ target, before: aRule, after: bRule })
    }
  }

  // Detectar removidas
  for (const [target, aRule] of aByTarget) {
    if (!bByTarget.has(target)) {
      removed.push(aRule)
    }
  }

  // reordered_only: mesmos targets, sem adicionadas/removidas/modificadas, mas ordem diferente
  const noContentChanges = added.length === 0 && removed.length === 0 && modified.length === 0
  const aTargets = a.map((r) => r.target)
  const bTargets = b.map((r) => r.target)
  const orderDiffers = noContentChanges && !aTargets.every((t, i) => t === bTargets[i])
  const reordered_only = orderDiffers

  return { reordered_only, added, removed, modified }
}
