/**
 * Testes de computeDiff
 * Cobre 10 cenários: added only, removed only, modified only, reordered only,
 * mixed, deep equality em value_map, source vs const, type_cast, required,
 * listas iguais produzem diff vazio.
 */

import { describe, it, expect } from "vitest"
import { computeDiff } from "@/lib/mappingDiff"
import type { MappingRule } from "@/types"

const r = (partial: Partial<MappingRule> & { target: string }): MappingRule => ({
  ...partial,
} as MappingRule)

describe("computeDiff", () => {
  it("retorna diff vazio quando listas são idênticas", () => {
    const rules: MappingRule[] = [
      r({ target: "a", source: "x" }),
      r({ target: "b", source: "y" }),
    ]
    const diff = computeDiff(rules, rules)
    expect(diff.added).toHaveLength(0)
    expect(diff.removed).toHaveLength(0)
    expect(diff.modified).toHaveLength(0)
    expect(diff.reordered_only).toBe(false)
  })

  it("detecta regra adicionada (added only)", () => {
    const a: MappingRule[] = [r({ target: "x", source: "field.x" })]
    const b: MappingRule[] = [
      r({ target: "x", source: "field.x" }),
      r({ target: "y", source: "field.y" }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.added).toHaveLength(1)
    expect(diff.added[0].target).toBe("y")
    expect(diff.removed).toHaveLength(0)
    expect(diff.modified).toHaveLength(0)
    expect(diff.reordered_only).toBe(false)
  })

  it("detecta regra removida (removed only)", () => {
    const a: MappingRule[] = [
      r({ target: "x", source: "f" }),
      r({ target: "z", source: "g" }),
    ]
    const b: MappingRule[] = [r({ target: "x", source: "f" })]
    const diff = computeDiff(a, b)
    expect(diff.removed).toHaveLength(1)
    expect(diff.removed[0].target).toBe("z")
    expect(diff.added).toHaveLength(0)
    expect(diff.modified).toHaveLength(0)
  })

  it("detecta regra modificada (modified only)", () => {
    const a: MappingRule[] = [r({ target: "ev.action", source: "action" })]
    const b: MappingRule[] = [r({ target: "ev.action", source: "action_new" })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
    expect(diff.modified[0].target).toBe("ev.action")
    expect(diff.modified[0].before.source).toBe("action")
    expect(diff.modified[0].after.source).toBe("action_new")
    expect(diff.added).toHaveLength(0)
    expect(diff.removed).toHaveLength(0)
  })

  it("detecta reordenação (reordered_only)", () => {
    const a: MappingRule[] = [
      r({ target: "a", source: "1" }),
      r({ target: "b", source: "2" }),
    ]
    const b: MappingRule[] = [
      r({ target: "b", source: "2" }),
      r({ target: "a", source: "1" }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.reordered_only).toBe(true)
    expect(diff.added).toHaveLength(0)
    expect(diff.removed).toHaveLength(0)
    expect(diff.modified).toHaveLength(0)
  })

  it("combina added + removed + modified (mixed)", () => {
    const a: MappingRule[] = [
      r({ target: "keep", source: "k" }),
      r({ target: "modify", source: "old" }),
      r({ target: "remove", source: "r" }),
    ]
    const b: MappingRule[] = [
      r({ target: "keep", source: "k" }),
      r({ target: "modify", source: "new" }),
      r({ target: "add", source: "a" }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.added).toHaveLength(1)
    expect(diff.added[0].target).toBe("add")
    expect(diff.removed).toHaveLength(1)
    expect(diff.removed[0].target).toBe("remove")
    expect(diff.modified).toHaveLength(1)
    expect(diff.modified[0].target).toBe("modify")
    expect(diff.reordered_only).toBe(false)
  })

  it("deep equality em value_map detecta mudança", () => {
    const a: MappingRule[] = [
      r({ target: "status", source: "s", value_map: { active: "ativo", inactive: "inativo" } }),
    ]
    const b: MappingRule[] = [
      r({ target: "status", source: "s", value_map: { active: "ativo", inactive: "desativado" } }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
    expect(diff.modified[0].before.value_map).toEqual({ active: "ativo", inactive: "inativo" })
    expect(diff.modified[0].after.value_map).toEqual({ active: "ativo", inactive: "desativado" })
  })

  it("deep equality em value_map identico não gera modificação", () => {
    const vm = { x: 1, y: 2 }
    const a: MappingRule[] = [r({ target: "f", source: "s", value_map: vm })]
    const b: MappingRule[] = [r({ target: "f", source: "s", value_map: { x: 1, y: 2 } })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(0)
  })

  it("mudança de source para const detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "ev.type", source: "type" })]
    const b: MappingRule[] = [r({ target: "ev.type", const: "login" })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("mudança em type_cast detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "ts", source: "timestamp" })]
    const b: MappingRule[] = [r({ target: "ts", source: "timestamp", type_cast: "iso_to_epoch" })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
    expect(diff.modified[0].after.type_cast).toBe("iso_to_epoch")
  })

  it("mudança em required detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "user", source: "u" })]
    const b: MappingRule[] = [r({ target: "user", source: "u", required: true })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("lista vazia para lista vazia — diff zerado sem reordered", () => {
    const diff = computeDiff([], [])
    expect(diff.added).toHaveLength(0)
    expect(diff.removed).toHaveLength(0)
    expect(diff.modified).toHaveLength(0)
    expect(diff.reordered_only).toBe(false)
  })

  // BUG-01: campos antes ignorados por rulesEqual.
  it("mudança em pre_cast detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "f", source: "s", pre_cast: "lowercase" })]
    const b: MappingRule[] = [r({ target: "f", source: "s", pre_cast: "uppercase" })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("mudança em fallback_source detecta modificação", () => {
    const a: MappingRule[] = [
      r({ target: "f", source: "s", fallback_source: ["a", "b"] }),
    ]
    const b: MappingRule[] = [
      r({ target: "f", source: "s", fallback_source: ["a", "c"] }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("adicionar fallback_source antes ausente detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "f", source: "s" })]
    const b: MappingRule[] = [r({ target: "f", source: "s", fallback_source: ["a"] })]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("mudança em when detecta modificação", () => {
    const a: MappingRule[] = [
      r({ target: "f", source: "s", when: { exists: "raw.x" } }),
    ]
    const b: MappingRule[] = [
      r({ target: "f", source: "s", when: { exists: "raw.y" } }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("mudança em expected_always_default detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "f", source: "s" })]
    const b: MappingRule[] = [
      r({ target: "f", source: "s", expected_always_default: true }),
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("ArrayBuilderRule: mudança em items detecta modificação", () => {
    const a: MappingRule[] = [
      {
        target: "observables",
        kind: "array_builder",
        items: [{ name: "src_ip", type: "IP Address", type_id: 2, source: "src.ip" }],
      },
    ]
    const b: MappingRule[] = [
      {
        target: "observables",
        kind: "array_builder",
        items: [{ name: "src_ip", type: "IP Address", type_id: 2, source: "src.ip2" }],
      },
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("ArrayBuilderRule: mudança em skip_null detecta modificação", () => {
    const a: MappingRule[] = [
      { target: "observables", kind: "array_builder", items: [], skip_null: true },
    ]
    const b: MappingRule[] = [
      { target: "observables", kind: "array_builder", items: [], skip_null: false },
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("ArrayBuilderRule: mudança em dedup_by detecta modificação", () => {
    const a: MappingRule[] = [
      { target: "observables", kind: "array_builder", items: [], dedup_by: ["value"] },
    ]
    const b: MappingRule[] = [
      { target: "observables", kind: "array_builder", items: [], dedup_by: ["value", "type"] },
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })

  it("ArrayBuilderRule idêntica (dedup_by ausente vs undefined) não gera modificação", () => {
    const a: MappingRule[] = [{ target: "observables", kind: "array_builder", items: [] }]
    const b: MappingRule[] = [
      { target: "observables", kind: "array_builder", items: [], dedup_by: undefined },
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(0)
  })

  it("mudar de scalar para array_builder no mesmo target detecta modificação", () => {
    const a: MappingRule[] = [r({ target: "observables", source: "x" })]
    const b: MappingRule[] = [
      { target: "observables", kind: "array_builder", items: [] },
    ]
    const diff = computeDiff(a, b)
    expect(diff.modified).toHaveLength(1)
  })
})
