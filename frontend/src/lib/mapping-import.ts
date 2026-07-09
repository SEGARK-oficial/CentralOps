/**
 * mapping-import
 * Validação e parsing de arquivos de exportação de regras de mapping.
 * Não depende de zod — validador manual para evitar nova dependência.
 */

import type { MappingRule, PreprocessOp } from "@/types"

// ── Schema version ─────────────────────────────────────────────────────────────

export const EXPORT_SCHEMA_VERSION = 2

// Versões de schema aceitas pelo parser (backward-compat).
// schema_version: 1 = legado (rules only).
// schema_version: 2 = com campo opcional `preprocess`.
const SUPPORTED_SCHEMA_VERSIONS = new Set([1, 2])

// ── Export shape ───────────────────────────────────────────────────────────────

export interface MappingExportMeta {
  id?: string
  vendor?: string
  event_type?: string
  name?: string
}

export interface MappingExport {
  schema_version: 1 | 2
  exported_at?: string
  mapping?: MappingExportMeta
  /** Presente apenas em schema_version: 2 — opcional mesmo em v2 (mapping pode não ter preprocess). */
  preprocess?: PreprocessOp[]
  rules: MappingRule[]
}

// ── Errors ────────────────────────────────────────────────────────────────────

export class MappingImportError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "MappingImportError"
  }
}

// ── Validator ─────────────────────────────────────────────────────────────────

/**
 * Validação de type_cast: apenas verifica que é uma string.
 * A whitelist foi removida — o backend (registry dinâmico) é a única fonte
 * de verdade para quais casts existem. Manter uma lista no frontend causava
 * divergência toda vez que novos casts eram adicionados ao backend.
 */
function validateRule(rule: unknown, index: number): MappingRule {
  if (typeof rule !== "object" || rule === null || Array.isArray(rule)) {
    throw new MappingImportError(`Regra ${index}: deve ser um objeto.`)
  }

  const r = rule as Record<string, unknown>

  if (typeof r.target !== "string" || !r.target.trim()) {
    throw new MappingImportError(`Regra ${index}: "target" é obrigatório e deve ser uma string não-vazia.`)
  }

  // source XOR const constraint
  const hasSource = "source" in r && r.source !== undefined && r.source !== null
  const hasConst = "const" in r && r.const !== undefined
  if (hasSource && hasConst) {
    throw new MappingImportError(
      `Regra ${index} ("${r.target}"): não pode ter "source" e "const" ao mesmo tempo.`,
    )
  }

  if (hasSource && typeof r.source !== "string") {
    throw new MappingImportError(`Regra ${index} ("${r.target}"): "source" deve ser uma string.`)
  }

  // Validação de type_cast: apenas shape (string), sem whitelist.
  // Validação de semântica é responsabilidade do backend (registry dinâmico).
  if ("type_cast" in r && r.type_cast !== null && r.type_cast !== undefined) {
    if (typeof r.type_cast !== "string") {
      throw new MappingImportError(
        `Regra ${index} ("${r.target}"): "type_cast" deve ser uma string.`,
      )
    }
  }

  // Validação de pre_cast: apenas shape (string), sem whitelist (mesma razão).
  if ("pre_cast" in r && r.pre_cast !== null && r.pre_cast !== undefined) {
    if (typeof r.pre_cast !== "string") {
      throw new MappingImportError(
        `Regra ${index} ("${r.target}"): "pre_cast" deve ser uma string.`,
      )
    }
  }

  if ("value_map" in r && r.value_map !== null && r.value_map !== undefined) {
    if (typeof r.value_map !== "object" || Array.isArray(r.value_map)) {
      throw new MappingImportError(
        `Regra ${index} ("${r.target}"): "value_map" deve ser um objeto JSON.`,
      )
    }
  }

  if ("required" in r && r.required !== undefined && typeof r.required !== "boolean") {
    throw new MappingImportError(
      `Regra ${index} ("${r.target}"): "required" deve ser true ou false.`,
    )
  }

  // Return the validated rule (cast — upstream validation guarantees shape)
  return r as unknown as MappingRule
}

/**
 * Valida uma operação de preprocess.
 * Hoje o único `op` suportado é `json_parse`; outros ops são rejeitados com
 * uma mensagem forward-compat orientando a atualizar o frontend.
 */
function validatePreprocessOp(op: unknown, index: number): PreprocessOp {
  if (typeof op !== "object" || op === null || Array.isArray(op)) {
    throw new MappingImportError(`preprocess[${index}]: deve ser um objeto.`)
  }

  const o = op as Record<string, unknown>

  if (o.op !== "json_parse") {
    throw new MappingImportError(
      `preprocess[${index}]: operação "${o.op}" não suportada nesta versão; atualize o frontend.`,
    )
  }

  if (typeof o.source !== "string" || !o.source.trim()) {
    throw new MappingImportError(
      `preprocess[${index}]: "source" é obrigatório e deve ser uma string não-vazia.`,
    )
  }

  if (typeof o.target !== "string" || !o.target.startsWith("_")) {
    throw new MappingImportError(
      `preprocess[${index}]: "target" deve ser uma string começando com "_".`,
    )
  }

  if ("tolerant" in o && o.tolerant !== undefined && typeof o.tolerant !== "boolean") {
    throw new MappingImportError(
      `preprocess[${index}]: "tolerant" deve ser true ou false.`,
    )
  }

  return o as unknown as PreprocessOp
}

/**
 * Parseia e valida o conteúdo JSON de um arquivo de exportação de regras.
 * Lança MappingImportError com mensagem legível em caso de falha.
 * Aceita schema_version: 1 (legado, sem preprocess) e 2 (com preprocess opcional).
 */
export function parseMappingExport(jsonText: string): MappingExport {
  let parsed: unknown
  try {
    parsed = JSON.parse(jsonText)
  } catch {
    throw new MappingImportError("Arquivo inválido: não é um JSON válido.")
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new MappingImportError("Arquivo inválido: o JSON deve ser um objeto.")
  }

  const obj = parsed as Record<string, unknown>

  // schema_version obrigatório e deve ser uma das versões suportadas
  if (typeof obj.schema_version !== "number" || !SUPPORTED_SCHEMA_VERSIONS.has(obj.schema_version)) {
    throw new MappingImportError(
      `Versão de schema inválida: esperado 1 ou 2, recebido ${JSON.stringify(obj.schema_version)}.`,
    )
  }

  // rules obrigatório e array
  if (!Array.isArray(obj.rules)) {
    throw new MappingImportError("Arquivo inválido: campo \"rules\" deve ser um array.")
  }

  const rules: MappingRule[] = obj.rules.map((rule, i) => validateRule(rule, i + 1))

  // Valida preprocess (apenas se presente — opcional mesmo em schema_version: 2)
  let preprocess: PreprocessOp[] | undefined = undefined
  if ("preprocess" in obj && obj.preprocess !== undefined && obj.preprocess !== null) {
    if (!Array.isArray(obj.preprocess)) {
      throw new MappingImportError(`Campo "preprocess" deve ser um array.`)
    }
    preprocess = obj.preprocess.map((op, i) => validatePreprocessOp(op, i + 1))
  }

  return {
    schema_version: obj.schema_version as 1 | 2,
    exported_at: typeof obj.exported_at === "string" ? obj.exported_at : undefined,
    mapping: typeof obj.mapping === "object" && obj.mapping !== null
      ? obj.mapping as MappingExportMeta
      : undefined,
    preprocess,
    rules,
  }
}

/**
 * Serializa as regras atuais (e ops de preprocess) num objeto de exportação
 * pronto para download.
 * Quando `preprocess` é não-vazio, emite schema_version: 2 com o bloco.
 * Quando vazio/ausente, ainda emite schema_version: 2 (forward-compat).
 */
export function buildMappingExport(
  rules: MappingRule[],
  meta?: MappingExportMeta,
  preprocess?: PreprocessOp[],
): MappingExport {
  return {
    schema_version: EXPORT_SCHEMA_VERSION,
    exported_at: new Date().toISOString(),
    mapping: meta,
    ...(preprocess && preprocess.length > 0 ? { preprocess } : {}),
    rules,
  }
}

/**
 * Gera nome de arquivo sugerido para o download.
 */
export function buildExportFilename(vendor?: string, eventType?: string): string {
  const parts = [vendor, eventType].filter(Boolean).join("-")
  const date = new Date().toISOString().slice(0, 10)
  return `mapping-rules${parts ? `-${parts}` : ""}-${date}.json`
}
