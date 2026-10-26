/**
 * Stub Community do seam @/ee/organizationSlots.
 *
 * Vincular/desvincular a organização do parceiro Sophos é Enterprise (ADR-0017):
 * a UI real só existe no overlay. No Community a linha da organização não ganha
 * nenhuma ação extra — o stub não renderiza nada.
 */

import { render } from "@testing-library/react"
import { describe, it, expect, vi } from "vitest"
import { OrganizationRowExtraActions } from "@/ee/organizationSlots"
import type { Organization } from "@/types"

const organization: Organization = {
  id: 10,
  name: "Acme Corp",
  slug: "acme-corp",
  is_active: true,
  integration_count: 1,
  auto_managed: true,
  external_provider: "sophos",
  partner_integration_id: 7,
}

describe("organizationSlots (stub CE)", () => {
  it("não renderiza nada, nem para organização gerida por parceiro", () => {
    const { container } = render(
      <OrganizationRowExtraActions organization={organization} onChanged={() => {}} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it("nunca chama onChanged por conta própria", () => {
    const onChanged = vi.fn()
    render(<OrganizationRowExtraActions organization={organization} onChanged={onChanged} />)
    expect(onChanged).not.toHaveBeenCalled()
  })
})
