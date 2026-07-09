import { describe, it, expect } from "vitest"

import { ERROR_KIND_OPTIONS } from "../QuarantineFiltersBar"

/**
 * Guarda de regressão: as opções de filtro de tipo de erro DEVEM usar os
 * mesmos valores que o backend emite e filtra. O backend faz match EXATO
 * (QuarantineEvent.error_kind == error_kind), então qualquer valor divergente
 * faz o filtro retornar vazio — foi o bug em que o conjunto antigo
 * (schema_error/missing_required/…) nunca casava com parse/map/validate/…
 *
 * Fonte da verdade: backend/app/collectors/quarantine.py
 *   ERROR_KIND_PARSE="parse", ERROR_KIND_MAP="map", ERROR_KIND_VALIDATE="validate",
 *   ERROR_KIND_MISSING_CUSTOMER_ID="missing_customer_id",
 *   ERROR_KIND_MISSING_MAPPING="missing_mapping".
 */
describe("QuarantineFiltersBar — ERROR_KIND_OPTIONS", () => {
  const BACKEND_ERROR_KINDS = new Set([
    "parse",
    "map",
    "validate",
    "missing_customer_id",
    "missing_mapping",
  ])

  it("toda opção exposta existe no enum de error_kind do backend", () => {
    for (const opt of ERROR_KIND_OPTIONS) {
      expect(BACKEND_ERROR_KINDS.has(opt.value)).toBe(true)
    }
  })

  it("não há valores duplicados e todo label é não-vazio", () => {
    const values = ERROR_KIND_OPTIONS.map((o) => o.value)
    expect(new Set(values).size).toBe(values.length)
    for (const opt of ERROR_KIND_OPTIONS) {
      expect(opt.label.trim().length).toBeGreaterThan(0)
    }
  })
})
