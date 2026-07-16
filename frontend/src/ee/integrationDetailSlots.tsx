import type { ReactElement } from "react"
import { CrownIcon, ExternalLinkIcon } from "lucide-react"
import { useTranslation } from "react-i18next"

import { Notice } from "@/components/ui/Notice/Notice"
import { DOCS } from "@/lib/docs"
import type { Integration } from "@/types"

/**
 * Enterprise integration-detail slot seam.
 *
 * This is the **Community stub**: the Sophos partner/organization auto-discovery UI
 * (PartnerTenantsPanel + AutoApprovePolicyModal — reseller multi-tenant management) is
 * an Enterprise feature and lives in the `@centralops/web-ee` overlay. The Enterprise
 * build overrides this module via Vite `resolve.alias`
 * (`@/ee/integrationDetailSlots` -> `@centralops/web-ee/integrationDetailSlots`), so
 * those panels render ONLY in the Enterprise bundle — the Community artifact ships none
 * of that code.
 *
 * Instead of rendering null (an invisible wall: the user creates a partner integration
 * and finds no surface explaining why tenants never import), the Community stub renders
 * a short Enterprise signpost for partner/organization integrations. Creating the
 * integration and discovering tenants stays Community by design — only the IMPORT
 * (materialization as Organizations + Integrations) is Enterprise.
 *
 * It is the counterpart of the `@/ee/routes` seam, but for panels embedded INSIDE a
 * Core page (IntegrationDetailPage) rather than a whole route.
 *
 * CONTRACT: the `IntegrationDetailExtraPanels` named export and its prop shape
 * (see `IntegrationDetailSlotProps`) MUST stay identical to the Enterprise
 * override — the EE build swaps this module wholesale by alias. (The override
 * re-declares the prop type locally instead of exporting it, to stay
 * import-free from Core; only the component export is part of the contract.)
 */
export interface IntegrationDetailSlotProps {
  integration: Integration
  isAdmin: boolean
  onRefreshIntegration: () => void | Promise<void>
}

export function IntegrationDetailExtraPanels({
  integration,
}: IntegrationDetailSlotProps): ReactElement | null {
  const { t } = useTranslation("integrations")
  if (integration.kind !== "partner" && integration.kind !== "organization") {
    return null
  }
  return (
    <Notice
      variant="info"
      icon={<CrownIcon size={16} />}
      title={t("enterprisePanel.title")}
      data-testid="enterprise-tenants-signpost"
    >
      <p>{t("enterprisePanel.body")}</p>
      <a
        href={DOCS.editionsUpgrade}
        target="_blank"
        rel="noopener noreferrer"
        className="mt-1 inline-flex items-center gap-1 font-medium underline"
      >
        {t("enterprisePanel.link")}
        <ExternalLinkIcon size={12} aria-hidden="true" />
      </a>
    </Notice>
  )
}
