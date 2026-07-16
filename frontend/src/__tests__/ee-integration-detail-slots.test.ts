import { createElement } from "react"
import { render, screen } from "@testing-library/react"
import { describe, expect, it } from "vitest"

import { IntegrationDetailExtraPanels } from "@/ee/integrationDetailSlots"
import type { Integration } from "@/types"

// The Community build renders no Enterprise integration-detail panels — the stub
// renders a short Enterprise SIGNPOST for partner/organization integrations
// (decisão: fim da "parede invisível") and nothing for other kinds. The actual
// partner/reseller UI (PartnerTenantsPanel) exists ONLY in the Enterprise build,
// which overrides @/ee/integrationDetailSlots (resolve.alias) with the web-ee overlay.
describe("IntegrationDetailExtraPanels seam", () => {
  const baseProps = {
    isAdmin: true,
    onRefreshIntegration: () => {},
  }

  it("Community: partner kind renders the Enterprise signpost, not the tenants UI", () => {
    render(
      createElement(IntegrationDetailExtraPanels, {
        ...baseProps,
        integration: { id: 1, kind: "partner" } as unknown as Integration,
      }),
    )
    expect(screen.getByTestId("enterprise-tenants-signpost")).toBeInTheDocument()
    // Nenhum controle de gestão de tenants no bundle Community.
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument()
  })

  it("Community: non-partner kinds render nothing", () => {
    const { container } = render(
      createElement(IntegrationDetailExtraPanels, {
        ...baseProps,
        integration: { id: 1, kind: "tenant" } as unknown as Integration,
      }),
    )
    expect(container).toBeEmptyDOMElement()
  })
})
