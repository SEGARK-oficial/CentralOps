/**
 * Stub Community do seam @/ee/integrationDetailSlots.
 *
 * A UI real de import de tenants (PartnerTenantsPanel) só existe no bundle
 * Enterprise (alias Vite). O stub CE deixou de ser "parede invisível"
 * (render null) e vira um signpost: para integrações kind=partner|organization
 * ele informa que a IMPORTAÇÃO de tenants é feature Enterprise + link de docs.
 * Para os demais kinds continua não renderizando nada.
 */

import { render, screen } from "@testing-library/react"
import { describe, it, expect, beforeAll } from "vitest"
import { IntegrationDetailExtraPanels } from "@/ee/integrationDetailSlots"
import i18n from "@/i18n"
import type { Integration } from "@/types"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function makeIntegration(kind: Integration["kind"]): Integration {
  return {
    id: 1,
    organization_id: 10,
    organization_name: "Org",
    name: "Sophos Partner Holding",
    platform: "sophos",
    is_active: true,
    is_authenticated: true,
    auth_status: "healthy",
    kind,
    capabilities: [],
  } as Integration
}

describe("integrationDetailSlots (stub CE)", () => {
  it("kind=partner renderiza o signpost Enterprise com link de upgrade", () => {
    render(
      <IntegrationDetailExtraPanels
        integration={makeIntegration("partner")}
        isAdmin
        onRefreshIntegration={() => {}}
      />,
    )
    const panel = screen.getByTestId("enterprise-tenants-signpost")
    expect(panel).toBeInTheDocument()
    expect(screen.getByText(/recurso Enterprise/i)).toBeInTheDocument()
    const link = screen.getByRole("link")
    expect(link).toHaveAttribute("href", expect.stringContaining("editions/upgrade"))
  })

  it("kind=organization também renderiza o signpost", () => {
    render(
      <IntegrationDetailExtraPanels
        integration={makeIntegration("organization")}
        isAdmin={false}
        onRefreshIntegration={() => {}}
      />,
    )
    expect(screen.getByTestId("enterprise-tenants-signpost")).toBeInTheDocument()
  })

  it("kind=tenant não renderiza nada (sem upsell fora do fluxo partner)", () => {
    const { container } = render(
      <IntegrationDetailExtraPanels
        integration={makeIntegration("tenant")}
        isAdmin
        onRefreshIntegration={() => {}}
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
