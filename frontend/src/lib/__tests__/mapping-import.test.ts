/**
 * Testes de mapping-import
 * Cobre: round-trip, schema_version, preprocess, type_cast, erros de validação.
 */

import { describe, it, expect } from "vitest"
import {
  parseMappingExport,
  buildMappingExport,
  buildExportFilename,
  MappingImportError,
  EXPORT_SCHEMA_VERSION,
} from "@/lib/mapping-import"
import type { MappingRule, PreprocessOp } from "@/types"

const RULES: MappingRule[] = [
  { target: "event.action", source: "data.action" },
  { target: "event.severity", source: "severity", required: true, type_cast: "to_str" },
  {
    target: "event.product",
    const: "Sophos Central",
  },
]

const PREPROCESS_OPS: PreprocessOp[] = [
  { op: "json_parse", source: "raw_data", target: "_parsed", tolerant: true },
]

// ── Round-trip ────────────────────────────────────────────────────────────────

describe("parseMappingExport — round-trip", () => {
  it("export → JSON string → parse → mesma representação", () => {
    const exported = buildMappingExport(RULES, { vendor: "sophos", event_type: "alert" })
    const json = JSON.stringify(exported)
    const parsed = parseMappingExport(json)

    expect(parsed.schema_version).toBe(EXPORT_SCHEMA_VERSION)
    expect(parsed.rules).toHaveLength(RULES.length)
    expect(parsed.rules[0].target).toBe("event.action")
    expect(parsed.rules[0].source).toBe("data.action")
    expect(parsed.rules[2].const).toBe("Sophos Central")
    expect(parsed.mapping?.vendor).toBe("sophos")
  })

  it("parsed.rules é uma cópia — mutações não afetam o original", () => {
    const exported = buildMappingExport(RULES)
    const parsed = parseMappingExport(JSON.stringify(exported))
    parsed.rules[0].target = "mutated"
    expect(RULES[0].target).toBe("event.action")
  })
})

// ── schema_version ────────────────────────────────────────────────────────────

describe("parseMappingExport — schema_version", () => {
  it("schema_version=1 é válido (legado, sem preprocess)", () => {
    const json = JSON.stringify({ schema_version: 1, rules: [] })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("schema_version=1 retorna preprocess undefined", () => {
    const json = JSON.stringify({ schema_version: 1, rules: [] })
    const parsed = parseMappingExport(json)
    expect(parsed.preprocess).toBeUndefined()
  })

  it("schema_version=2 é válido", () => {
    const json = JSON.stringify({ schema_version: 2, rules: [] })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("schema_version=2 com preprocess é válido", () => {
    const json = JSON.stringify({
      schema_version: 2,
      preprocess: [{ op: "json_parse", source: "raw", target: "_out", tolerant: true }],
      rules: [],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("schema_version=2 sem preprocess é válido (preprocess é opcional)", () => {
    const json = JSON.stringify({ schema_version: 2, rules: [] })
    const parsed = parseMappingExport(json)
    expect(parsed.preprocess).toBeUndefined()
  })

  it("schema_version=3 lança MappingImportError (versão futura não suportada)", () => {
    const json = JSON.stringify({ schema_version: 3, rules: [] })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/schema/i)
  })

  it("schema_version ausente lança MappingImportError", () => {
    const json = JSON.stringify({ rules: [] })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
  })

  it("EXPORT_SCHEMA_VERSION é 2", () => {
    expect(EXPORT_SCHEMA_VERSION).toBe(2)
  })
})

// ── preprocess export ─────────────────────────────────────────────────────────

describe("buildMappingExport — preprocess", () => {
  it("com preprocess não-vazio inclui o campo no output", () => {
    const exported = buildMappingExport(RULES, undefined, PREPROCESS_OPS)
    expect(exported.preprocess).toBeDefined()
    expect(exported.preprocess).toHaveLength(1)
    expect(exported.preprocess![0].op).toBe("json_parse")
  })

  it("sem preprocess omite o campo (JSON mais limpo)", () => {
    const exported = buildMappingExport(RULES)
    expect(exported.preprocess).toBeUndefined()
    // Garante que o campo não está presente nem como undefined no JSON
    const json = JSON.stringify(exported)
    expect(json).not.toContain("preprocess")
  })

  it("com preprocess vazio [] omite o campo (forward-compat)", () => {
    const exported = buildMappingExport(RULES, undefined, [])
    expect(exported.preprocess).toBeUndefined()
    const json = JSON.stringify(exported)
    expect(json).not.toContain("preprocess")
  })

  it("round-trip com preprocess: export → parse → preprocess preservado", () => {
    const exported = buildMappingExport(RULES, { vendor: "sophos" }, PREPROCESS_OPS)
    const parsed = parseMappingExport(JSON.stringify(exported))
    expect(parsed.preprocess).toHaveLength(1)
    expect(parsed.preprocess![0].op).toBe("json_parse")
    expect(parsed.preprocess![0].source).toBe("raw_data")
    expect(parsed.preprocess![0].target).toBe("_parsed")
    expect(parsed.preprocess![0].tolerant).toBe(true)
  })
})

// ── preprocess import validation ──────────────────────────────────────────────

describe("parseMappingExport — validação de preprocess", () => {
  it("preprocess não-array lança MappingImportError", () => {
    const json = JSON.stringify({ schema_version: 2, preprocess: "not-array", rules: [] })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/preprocess/i)
  })

  it("op desconhecido lança MappingImportError com orientação de atualização", () => {
    const json = JSON.stringify({
      schema_version: 2,
      preprocess: [{ op: "future_op", source: "x", target: "_y", tolerant: true }],
      rules: [],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/não suportada.*atualize/i)
  })

  it("target sem prefixo '_' lança MappingImportError", () => {
    const json = JSON.stringify({
      schema_version: 2,
      preprocess: [{ op: "json_parse", source: "raw", target: "parsed", tolerant: true }],
      rules: [],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/"target"/)
  })

  it("source vazio lança MappingImportError", () => {
    const json = JSON.stringify({
      schema_version: 2,
      preprocess: [{ op: "json_parse", source: "", target: "_out", tolerant: true }],
      rules: [],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/"source"/)
  })

  it("tolerant não-booleano lança MappingImportError", () => {
    const json = JSON.stringify({
      schema_version: 2,
      preprocess: [{ op: "json_parse", source: "raw", target: "_out", tolerant: "yes" }],
      rules: [],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/"tolerant"/)
  })
})

// ── Regra source + const ──────────────────────────────────────────────────────

describe("parseMappingExport — restrição source XOR const", () => {
  it("regra com source E const lança MappingImportError", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.action", source: "data.action", const: "fixed" }],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/source.*const|const.*source/i)
  })

  it("regra só com source é válida", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.action", source: "data.action" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("regra só com const é válida", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.product", const: "fixed" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })
})

// ── Validação de type_cast (sem whitelist) ────────────────────────────────────

describe("parseMappingExport — type_cast (sem whitelist)", () => {
  it("type_cast com valor string qualquer não lança erro (sem whitelist)", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.ts", source: "ts", type_cast: "unix_to_iso" }],
    })
    // Antes lançava erro; agora a validação de semântica é do backend
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast com um dos 7 novos casts (score_to_percent) é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.score", source: "risk", type_cast: "score_to_percent" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast lowercase é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.name", source: "name", type_cast: "lowercase" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast uppercase é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.name", source: "name", type_cast: "uppercase" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast trim é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.name", source: "name", type_cast: "trim" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast to_array é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.tags", source: "tags", type_cast: "to_array" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast dedup é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.tags", source: "tags", type_cast: "dedup" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast mitre_tactic_to_ocsf é aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.tactic", source: "tactic", type_cast: "mitre_tactic_to_ocsf" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast não-string (número) lança MappingImportError (validação de shape)", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.ts", source: "ts", type_cast: 4 }],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
    expect(() => parseMappingExport(json)).toThrow(/type_cast/)
  })

  it("type_cast não-string (array) lança MappingImportError", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.ts", source: "ts", type_cast: [] }],
    })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
  })

  it("type_cast null é aceito (campo opcional ausente)", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.ts", source: "ts", type_cast: null }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })

  it("type_cast válido clássico (iso_to_epoch) continua aceito", () => {
    const json = JSON.stringify({
      schema_version: 1,
      rules: [{ target: "event.ts", source: "ts", type_cast: "iso_to_epoch" }],
    })
    expect(() => parseMappingExport(json)).not.toThrow()
  })
})

// ── Validação de campos ───────────────────────────────────────────────────────

describe("parseMappingExport — validação de campos", () => {
  it("target ausente ou vazio lança MappingImportError", () => {
    const json = JSON.stringify({ schema_version: 1, rules: [{ source: "x" }] })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
  })

  it("JSON inválido lança MappingImportError", () => {
    expect(() => parseMappingExport("{ invalido }")).toThrow(MappingImportError)
    expect(() => parseMappingExport("{ invalido }")).toThrow(/JSON/)
  })

  it("JSON não-objeto (array) lança MappingImportError", () => {
    expect(() => parseMappingExport("[1, 2]")).toThrow(MappingImportError)
  })

  it("rules não-array lança MappingImportError", () => {
    const json = JSON.stringify({ schema_version: 1, rules: "not-array" })
    expect(() => parseMappingExport(json)).toThrow(MappingImportError)
  })
})

// ── buildExportFilename ───────────────────────────────────────────────────────

describe("buildExportFilename", () => {
  it("sem vendor/event_type retorna mapping-rules-<date>.json", () => {
    const name = buildExportFilename()
    expect(name).toMatch(/^mapping-rules-\d{4}-\d{2}-\d{2}\.json$/)
  })

  it("com vendor e event_type inclui ambos no nome", () => {
    const name = buildExportFilename("sophos", "alert")
    expect(name).toMatch(/^mapping-rules-sophos-alert-/)
  })
})
