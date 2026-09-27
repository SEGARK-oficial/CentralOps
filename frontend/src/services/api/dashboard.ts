import { apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type {
  DashboardSummaryV2,
  PlatformType,
} from "@/types"

// ── Dashboard API ─────────────────────────────────────────────────────

/**
 * GET /dashboard/summary — payload v2 consolidado (fetch ÚNICA do dashboard).
 * O shape v1 (Accept: application/vnd.centralops.v1+json) foi removido junto
 * com a superfície de alertas Wazuh-only.
 */
export async function getDashboardSummary(
  params?: {
    organization_id?: number | null
    integration_id?: number | null
    platform?: PlatformType | null
    days?: number
  },
  options?: Pick<ApiRequestOptions, "signal">,
) {
  const searchParams = new URLSearchParams()
  if (params?.organization_id) searchParams.set("organization_id", String(params.organization_id))
  if (params?.integration_id) searchParams.set("integration_id", String(params.integration_id))
  if (params?.platform) searchParams.set("platform", params.platform)
  if (params?.days) searchParams.set("days", String(params.days))
  const qs = searchParams.toString()
  return apiRequest<DashboardSummaryV2>(`/dashboard/summary${qs ? `?${qs}` : ""}`, options)
}

