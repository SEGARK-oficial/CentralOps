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
})
