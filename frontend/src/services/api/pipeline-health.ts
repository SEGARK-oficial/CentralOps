import { apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type { IntegrationPipelineHealth } from "@/types"

// ── Sprint 5: Pipeline Health API ─────────────────────────────────────────────


export async function getIntegrationPipelineHealth(
  integrationId: number,
  options?: Pick<ApiRequestOptions, "signal"> & { bypassCache?: boolean },
): Promise<IntegrationPipelineHealth> {
  const headers: Record<string, string> = {}
  if (options?.bypassCache) {
    headers["Cache-Control"] = "no-cache"
  }
  const { bypassCache: _bypass, ...restOptions } = options ?? {}
  return apiRequest<IntegrationPipelineHealth>(
    `/integrations/${encodeURIComponent(integrationId)}/pipeline-health`,
    { ...restOptions, headers },
  )
}

export async function listPipelineHealth(
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<IntegrationPipelineHealth[]> {
  // Backend retorna BulkPipelineHealthResponse {items, total, cached_at}.
  // Desempacota items[] para manter contrato simples no frontend.
  const response = await apiRequest<{
    items: IntegrationPipelineHealth[]
    total: number
    cached_at: string
  }>("/integrations/pipeline-health", options)
  return Array.isArray(response?.items) ? response.items : []
}

