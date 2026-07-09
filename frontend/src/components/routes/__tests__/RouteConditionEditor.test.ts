import { describe, it, expect } from "vitest"
import { clausesToCondition, conditionToClauses } from "../RouteConditionEditor"

describe("clausesToCondition", () => {
  it("collapses a lone eq to a scalar shorthand", () => {
    expect(clausesToCondition([{ field: "vendor", op: "eq", value: "sophos" }])).toEqual({ vendor: "sophos" })
  })

  it("coerces numeric fields to numbers", () => {
    expect(clausesToCondition([{ field: "severity_id", op: "gte", value: "4" }])).toEqual({ severity_id: { gte: 4 } })
  })

  it("FOLDS a pre-existing eq scalar into the op-map (eq-first, review MEDIUM)", () => {
    // eq first, then another op on the same field — the eq must NOT be dropped.
    expect(
      clausesToCondition([
        { field: "vendor", op: "eq", value: "sophos" },
        { field: "vendor", op: "ne", value: "x" },
      ]),
    ).toEqual({ vendor: { eq: "sophos", ne: "x" } })
    expect(
      clausesToCondition([
        { field: "severity_id", op: "eq", value: "3" },
        { field: "severity_id", op: "gte", value: "4" },
      ]),
    ).toEqual({ severity_id: { eq: 3, gte: 4 } })
  })

  it("is order-independent for eq + op merge", () => {
    const a = clausesToCondition([
      { field: "severity_id", op: "gte", value: "4" },
      { field: "severity_id", op: "eq", value: "3" },
    ])
    const b = clausesToCondition([
      { field: "severity_id", op: "eq", value: "3" },
      { field: "severity_id", op: "gte", value: "4" },
    ])
    expect(a).toEqual({ severity_id: { gte: 4, eq: 3 } })
    expect(b).toEqual({ severity_id: { eq: 3, gte: 4 } })
  })

  it("handles in/nin as arrays and exists as bool", () => {
    expect(clausesToCondition([{ field: "vendor", op: "in", value: "a, b ,c" }])).toEqual({ vendor: { in: ["a", "b", "c"] } })
    expect(clausesToCondition([{ field: "vendor", op: "exists", value: "false" }])).toEqual({ vendor: { exists: false } })
  })

  it("empty clauses → catch-all {}", () => {
    expect(clausesToCondition([])).toEqual({})
  })
})

describe("conditionToClauses round-trip", () => {
  it("round-trips an {eq, gte} op-map back to identity", () => {
    const cond = { severity_id: { eq: 5, gte: 3 } }
    expect(clausesToCondition(conditionToClauses(cond))).toEqual(cond)
  })

  it("round-trips a scalar shorthand", () => {
    const cond = { vendor: "sophos" }
    expect(clausesToCondition(conditionToClauses(cond))).toEqual(cond)
  })
})
