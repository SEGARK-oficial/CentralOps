import type { ReactElement } from "react"

import type { Integration } from "@/types"

/**
 * Enterprise integration-detail slot seam.
 *
 * This is the **Community stub**: it renders nothing. The Sophos partner/organization
 * auto-discovery UI (PartnerTenantsPanel + AutoApprovePolicyModal — reseller
 * multi-tenant management) is an Enterprise feature and lives in the `@centralops/web-ee`
 * overlay. The Enterprise build overrides this module via Vite `resolve.alias`
 * (`@/ee/integrationDetailSlots` -> `@centralops/web-ee/integrationDetailSlots`), so
 * those panels render ONLY in the Enterprise bundle — the Community artifact ships none
 * of that code.
 *
 * It is the counterpart of the `@/ee/routes` seam, but for panels embedded INSIDE a
 * Core page (IntegrationDetailPage) rather than a whole route.
 */
export interface IntegrationDetailSlotProps {
  integration: Integration
  isAdmin: boolean
  onRefreshIntegration: () => void | Promise<void>
}

export function IntegrationDetailExtraPanels(
  _props: IntegrationDetailSlotProps,
): ReactElement | null {
  return null
}
