import { describe, expect, it } from "vitest"

import { eeRoutes } from "@/ee/routes"

// The Community build ships an empty EE route set — no
// Enterprise screen/chunk in the Community bundle. The Enterprise build overrides
// `@/ee/routes` (resolve.alias) with the real overlay routes.
describe("EE routes seam", () => {
  it("is an empty route set in the Community build", () => {
    expect(Array.isArray(eeRoutes)).toBe(true)
    expect(eeRoutes).toHaveLength(0)
  })
})
