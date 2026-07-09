import { describe, expect, it } from "vitest"

import { IntegrationDetailExtraPanels } from "@/ee/integrationDetailSlots"

// The Community build renders no Enterprise integration-detail
// panels — the stub returns null, so no partner/reseller UI is in the Community bundle.
// The Enterprise build overrides @/ee/integrationDetailSlots (resolve.alias) with the
// web-ee overlay that renders PartnerTenantsPanel.
describe("IntegrationDetailExtraPanels seam", () => {
  it("renders nothing in the Community build", () => {
    const result = IntegrationDetailExtraPanels({
      // minimal stand-in — the stub ignores its props
      integration: { id: 1, kind: "partner" } as never,
      isAdmin: true,
      onRefreshIntegration: () => {},
    })
    expect(result).toBeNull()
  })
})
