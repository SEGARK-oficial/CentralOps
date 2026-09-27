import { ADMIN_REDIRECT_PATH, V1_ACCEPT_HEADER, apiRequest } from "./_core"
import type {
  AutoApprovePolicyResponse,
  CreateIntegrationRequest,
  DiscoveredTenant,
  HealthResponse,
  Integration,
  IntegrationCollectionFilters,
  IntegrationHealth,
  IntegrationOverview,
  PartnerSyncResult,
  PartnerSyncStatus,
  ProviderPlatformRead,
  SophosTenantListResponse,
  SophosTenantSelectResponse,
  TenantSelectionState,
  TestConnectionResponse,
  UpdateIntegrationRequest,
} from "@/types"

// ── Integration API ───────────────────────────────────────────────────

export interface ListIntegrationsFilters {
  organizationId?: number
  platform?: string
  includeInactive?: boolean
  /** Substring case-insensitive em integration.name. */
  name?: string
  /** 'tenant' | 'partner' | 'organization' | 'all'. */
  kind?: "tenant" | "partner" | "organization" | "all"
  /** 'active' | 'inactive' | 'all'. Default servidor: 'active'. */
  status?: "active" | "inactive" | "all"
  region?: string
  dataGeography?: string
  /** Página 1-based. */
  page?: number
  /** Tamanho da página (max 200). */
  size?: number
}

export async function listIntegrations(
  organizationIdOrFilters?: number | ListIntegrationsFilters,
  platform?: string,
  includeInactive = false,
) {
  // Compat: chamadas antigas (orgId, platform, includeInactive) continuam
  // funcionando. Novo formato: listIntegrations({...filters}).
  const filters: ListIntegrationsFilters =
    typeof organizationIdOrFilters === "object" && organizationIdOrFilters !== null
      ? organizationIdOrFilters
      : {
          organizationId: organizationIdOrFilters,
          platform,
          includeInactive,
        }

  const params = new URLSearchParams()
  if (filters.organizationId) params.set("organization_id", String(filters.organizationId))
  if (filters.platform) params.set("platform", filters.platform)
  if (filters.includeInactive) params.set("include_inactive", "true")
  if (filters.name && filters.name.trim()) params.set("name", filters.name.trim())
  if (filters.kind) params.set("kind", filters.kind)
  if (filters.status) params.set("status", filters.status)
  if (filters.region && filters.region.trim()) params.set("region", filters.region.trim())
  if (filters.dataGeography && filters.dataGeography.trim()) {
    params.set("data_geography", filters.dataGeography.trim())
  }
  if (filters.page && filters.page > 0) params.set("page", String(filters.page))
  if (filters.size && filters.size > 0) params.set("size", String(filters.size))

  const qs = params.toString()
  return apiRequest<Integration[]>(`/integrations/${qs ? `?${qs}` : ""}`)
}

export interface BulkDeactivateIntegrationsResult {
  processed: number
  deactivated: number
  errors: { id: number; reason: string }[]
}

export async function bulkDeactivateIntegrations(ids: number[]) {
  return apiRequest<BulkDeactivateIntegrationsResult>(
    "/integrations/bulk/deactivate",
    {
      method: "POST",
      body: JSON.stringify({ ids }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function getIntegration(id: number) {
  return apiRequest<Integration>(`/integrations/${encodeURIComponent(id)}`)
}

export async function createIntegration(data: CreateIntegrationRequest) {
  return apiRequest<Integration>("/integrations/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateIntegration(id: number, data: UpdateIntegrationRequest) {
  return apiRequest<Integration>(`/integrations/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export interface DeleteIntegrationOptions {
  /** Soft-delete cascade for Partner integrations with active children. */
  force?: boolean
  /** Hard-delete (admin only); cannot be combined with ``force``. */
  purge?: boolean
}

export interface DeleteIntegrationResult {
  detail: string
  affected?: number
}

export async function deleteIntegration(id: number, options: DeleteIntegrationOptions = {}) {
  const params = new URLSearchParams()
  if (options.force) params.set("force", "true")
  if (options.purge) params.set("purge", "true")
  const qs = params.toString()
  return apiRequest<DeleteIntegrationResult>(`/integrations/${encodeURIComponent(id)}${qs ? `?${qs}` : ""}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Sophos Partner Mode ─────────────────────────────────────────────

export async function syncPartnerTenants(id: number) {
  return apiRequest<PartnerSyncResult>(`/integrations/${encodeURIComponent(id)}/sync-tenants`, {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getPartnerSyncStatus(id: number) {
  return apiRequest<PartnerSyncStatus>(`/integrations/${encodeURIComponent(id)}/sync-status`)
}

export async function listDiscoveredTenants(id: number, includeInactive = false) {
  const params = new URLSearchParams()
  if (includeInactive) params.set("include_inactive", "true")
  const qs = params.toString()
  return apiRequest<DiscoveredTenant[]>(
    `/integrations/${encodeURIComponent(id)}/discovered-tenants${qs ? `?${qs}` : ""}`,
  )
}

// ── Tenant selection ────────────────────────────────────────

export interface ListSophosTenantsOptions {
  /** Quando true, força chamada Sophos `/partner/v1/tenants` (10–30s, cache 5min). */
  refresh?: boolean
  page?: number
  size?: number
  state?: TenantSelectionState | "all"
  search?: string
  geography?: string
}

export async function listSophosTenants(
  partnerId: number,
  opts: ListSophosTenantsOptions = {},
) {
  const params = new URLSearchParams()
  if (opts.refresh) params.set("refresh", "true")
  if (opts.page !== undefined) params.set("page", String(opts.page))
  if (opts.size !== undefined) params.set("size", String(opts.size))
  if (opts.state) params.set("state", opts.state)
  if (opts.search) params.set("search", opts.search)
  if (opts.geography) params.set("geography", opts.geography)
  const qs = params.toString()
  return apiRequest<SophosTenantListResponse>(
    `/integrations/${encodeURIComponent(partnerId)}/sophos-tenants${qs ? `?${qs}` : ""}`,
  )
}

export async function selectTenants(
  partnerId: number,
  externalIds: string[],
  state: Extract<TenantSelectionState, "approved" | "excluded">,
) {
  return apiRequest<SophosTenantSelectResponse>(
    `/integrations/${encodeURIComponent(partnerId)}/tenants/select`,
    {
      method: "POST",
      body: JSON.stringify({ external_ids: externalIds, state }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function updateAutoApprovePolicy(
  partnerId: number,
  autoApprove: boolean,
) {
  return apiRequest<AutoApprovePolicyResponse>(
    `/integrations/${encodeURIComponent(partnerId)}/auto-approve-policy`,
    {
      method: "PATCH",
      body: JSON.stringify({ auto_approve_new_tenants: autoApprove }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function testIntegrationConnection(id: number) {
  return apiRequest<TestConnectionResponse>(`/integrations/${encodeURIComponent(id)}/test-connection`, {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getIntegrationHealth(id: number) {
  return apiRequest<IntegrationHealth>(`/integrations/${encodeURIComponent(id)}/health`, {
    headers: V1_ACCEPT_HEADER,
  })
}

export async function getIntegrationHealthV2(id: number) {
  return apiRequest<HealthResponse>(`/integrations/${encodeURIComponent(id)}/health`)
}

export async function getProviderPlatforms() {
  return apiRequest<ProviderPlatformRead[]>("/providers/platforms")
}

/** Testa credenciais CRUAS (pré-save) de uma plataforma — stateless, não persiste. */
export async function testProviderConnection(
  platform: string,
  config: Record<string, unknown>,
) {
  return apiRequest<{ ok: boolean; detail: string; latency_ms?: number | null }>(
    `/providers/${encodeURIComponent(platform)}/test-connection`,
    { method: "POST", body: JSON.stringify({ config }) },
  )
}

// ── Filtros de coleta (descarte na origem) ────────────────────────────

/**
 * Filtros gravados desta integração + o schema declarado pelos plugins.
 *
 * Vêm juntos de propósito: a tela não precisa cruzar com
 * `GET /providers/platforms`, e `filters` já chega revalidado (valor que não
 * passa mais na declaração atual do plugin é OMITIDO, porque o coletor também o
 * ignora — ecoá-lo mostraria uma redução de volume que não está acontecendo).
 */
export async function getIntegrationCollectionFilters(integrationId: number) {
  return apiRequest<IntegrationCollectionFilters>(
    `/integrations/${encodeURIComponent(integrationId)}/collection-filters`,
  )
}

/**
 * SUBSTITUI toda a configuração de filtros da integração — não é merge.
 * Stream ausente do corpo perde os filtros que tinha e `{}` limpa tudo; é assim
 * que "remover filtro" funciona sem endpoint de delete.
 *
 * Validação fail-closed no backend: valor fora do contrato do plugin volta 422
 * com a chave e a faixa violadas, nunca um campo silenciosamente descartado.
 */
export async function updateIntegrationCollectionFilters(
  integrationId: number,
  filters: IntegrationCollectionFilters["filters"],
) {
  return apiRequest<IntegrationCollectionFilters>(
    `/integrations/${encodeURIComponent(integrationId)}/collection-filters`,
    { method: "PUT", body: JSON.stringify({ filters }) },
  )
}

export async function getIntegrationOverview(id: number) {
  return apiRequest<IntegrationOverview>(`/integrations/${encodeURIComponent(id)}/overview`)
}

export async function listSupportedPlatforms() {
  return apiRequest<{ platforms: string[] }>("/integrations/platforms")
}

