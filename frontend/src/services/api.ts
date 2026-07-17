/**
 * API Service
 * Serviço para comunicação com o backend FastAPI
 */

import i18n from "@/i18n"
import type {
  AppUser,
  AccountProfile,
  SelfProfileUpdate,
  PasswordChangeRequest,
  PasswordChangeResult,
  RevokeOtherSessionsResult,
  DashboardSummaryV2,
  HealthResponse,
  ProviderPlatformRead,
  AuditFilters,
  AuditHistoryItem,
  AuthStatus,
  AuthUser,
  BootstrapAdminRequest,
  CollectionState,
  CollectorAuditResponse,
  CollectorConfig,
  CollectorConfigTestResponse,
  CollectorSummary,
  CollectorTriggerResponse,
  CollectorVendor,
  UpdateCollectorConfigRequest,
  CreateEmailRequest,
  CreateIntegrationRequest,
  CreateOrganizationRequest,
  AutoApprovePolicyResponse,
  DiscoveredTenant,
  PartnerSyncResult,
  PartnerSyncStatus,
  SophosTenantListResponse,
  SophosTenantSelectResponse,
  TenantSelectionState,
  CreateQueryRequest,
  CreateScheduleRequest,
  CreateUserRequest,
  EmailConfig,
  EmailRecipient,
  Integration,
  IntegrationHealth,
  IdentityConfig,
  IdentityConnectionTestResult,
  EntraSyncStatus,
  EntraSyncTriggerResult,
  IntegrationOverview,
  LoginRequest,
  LoginResponse,
  UpdateIdentityConfigRequest,
  Organization,
  PlatformType,
  Query,
  Schedule,
  SearchHistoryItem,
  TestConnectionResponse,
  UpdateEmailConfigRequest,
  UpdateIntegrationRequest,
  UpdateOrganizationRequest,
  UpdateQueryRequest,
  UpdateUserRequest,
  TypeCastDescriptor,
  QueryCapabilityRead,
  QueryJobRead,
  QueryJobSubmitRequest,
  DetectionRead,
  DetectionStatusUpdate,
  CorrelationRuleRead,
  CorrelationRuleCreate,
  CorrelationRuleUpdate,
  CaptureSession,
  CaptureSessionList,
  CaptureEventList,
  CaptureSessionStartRequest,
  EditionStatus,
  LicenseStatus,
  OcsfPolicy,
  OcsfEnforcementMode,
  OcsfCompliance,
} from "@/types"

const BASE_URL = import.meta.env.VITE_BACKEND_URL || "/api"
const ADMIN_REDIRECT_PATH = "/search"
const V1_ACCEPT_HEADER = { Accept: "application/vnd.centralops.v1+json" } as const

interface ApiRequestOptions extends RequestInit {
  forbiddenRedirectTo?: string
}

export class ApiRequestError extends Error {
  statusCode: number
  code?: string
  details?: Record<string, unknown>

  constructor(message: string, statusCode: number, code?: string, details?: Record<string, unknown>) {
    super(message)
    this.name = "ApiRequestError"
    this.statusCode = statusCode
    this.code = code
    this.details = details
  }
}

// Helper para fazer requests
async function apiRequest<T>(endpoint: string, options: ApiRequestOptions = {}): Promise<T> {
  const url = `${BASE_URL}${endpoint}`
  const { forbiddenRedirectTo, ...requestOptions } = options

  const defaultHeaders: Record<string, string> = {
    "Content-Type": "application/json",
    // tell the backend the user's chosen language so localized
    // API errors and emails come back in it. i18n.language is a base code
    // (pt/en/es); the backend's Accept-Language parser resolves it.
    "Accept-Language": i18n.language || "pt",
  }

  const response = await fetch(url, {
    credentials: "include",
    headers: {
      ...defaultHeaders,
      ...requestOptions.headers,
    },
    ...requestOptions,
  })

  if (!response.ok) {
    if (response.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("app-auth-expired"))
    }

    if (response.status === 403 && forbiddenRedirectTo && typeof window !== "undefined") {
      window.dispatchEvent(
        new CustomEvent("app-api-forbidden", {
          detail: { redirectTo: forbiddenRedirectTo },
        }),
      )
    }

    let errorMessage = `HTTP error! status: ${response.status}`
    let errorCode: string | undefined
    let errorDetails: Record<string, unknown> | undefined
    try {
      const errorData = await response.json()
      const structuredError = errorData?.error ?? (typeof errorData?.detail === "object" ? errorData.detail?.error : undefined)
      if (structuredError && typeof structuredError === "object") {
        if (typeof structuredError.message === "string") {
          errorMessage = structuredError.message
        }
        if (typeof structuredError.code === "string") {
          errorCode = structuredError.code
        }
        if (structuredError.details && typeof structuredError.details === "object") {
          errorDetails = structuredError.details as Record<string, unknown>
        }
      } else if (typeof errorData?.detail === "string") {
        errorMessage = errorData.detail
      } else if (errorData?.detail) {
        errorMessage = JSON.stringify(errorData.detail)
      } else if (typeof errorData?.message === "string") {
        errorMessage = errorData.message
      }
    } catch {
      // Se não conseguir fazer parse do JSON, usar mensagem padrão
    }
    throw new ApiRequestError(errorMessage, response.status, errorCode, errorDetails)
  }

  // Handle empty responses (like DELETE)
  if (response.status === 204) {
    return {} as T
  }

  return response.json()
}

// App auth API functions
export async function getAuthStatus() {
  return apiRequest<AuthStatus>("/auth/status")
}

/**
 * URL absoluta do início do fluxo SSO (Microsoft Entra). O browser navega
 * diretamente para cá (top-level) — não é um fetch. Usa BASE_URL para
 * funcionar tanto em produção (mesmo domínio) quanto no dev server.
 */
export function ssoLoginUrl(): string {
  return `${BASE_URL}/auth/sso/login`
}

// ── Identity / SSO config (admin) ─────────────────────────────────────
export async function getIdentityConfig() {
  return apiRequest<IdentityConfig>("/identity/config")
}

export async function updateIdentityConfig(data: UpdateIdentityConfigRequest) {
  return apiRequest<IdentityConfig>("/identity/config", {
    method: "PUT",
    body: JSON.stringify(data),
  })
}

export async function testIdentityConnection() {
  return apiRequest<IdentityConnectionTestResult>("/identity/config/test", {
    method: "POST",
  })
}

// disparar sync manual de usuários do Entra via Graph
export async function syncEntraNow() {
  return apiRequest<EntraSyncTriggerResult>("/identity/config/sync", {
    method: "POST",
  })
}

// status do último sync de usuários do Entra
export async function getEntraSyncStatus() {
  return apiRequest<EntraSyncStatus>("/identity/config/sync-status")
}

export async function bootstrapAdmin(data: BootstrapAdminRequest) {
  return apiRequest<LoginResponse>("/auth/bootstrap", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function login(data: LoginRequest) {
  return apiRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function logout() {
  return apiRequest<{ detail: string }>("/auth/logout", {
    method: "POST",
  })
}

export async function getCurrentUser() {
  return apiRequest<AuthUser>("/auth/me")
}

/** Persist the UI language on the user's profile so it follows them across
 *  devices. Best-effort — callers ignore failures (e.g. 401
 *  when called from the pre-login language switcher). */
export async function updateMyLocale(locale: string): Promise<AuthUser> {
  return apiRequest<AuthUser>("/auth/me/locale", {
    method: "PUT",
    body: JSON.stringify({ locale }),
  })
}

export async function verifyAdminAccess() {
  return apiRequest<{ allowed: boolean }>("/auth/admin-access")
}

// ── Self-service account (própria conta) ──────────────────────────────

/** Perfil completo do próprio usuário (com created_at/last_login_at) para a
 *  página de conta. Só lê a identidade do caller — sem escopo de org. */
export async function getMyProfile(): Promise<AccountProfile> {
  return apiRequest<AccountProfile>("/auth/me/profile")
}

/** Atualiza os campos que o usuário pode alterar em si mesmo (display_name/
 *  email/locale). Trocar o e-mail exige `current_password` (reautenticação). */
export async function updateMyProfile(data: SelfProfileUpdate): Promise<AccountProfile> {
  return apiRequest<AccountProfile>("/auth/me", {
    method: "PATCH",
    body: JSON.stringify(data),
  })
}

/** Troca a própria senha (contas locais). Revoga as demais sessões e mantém a
 *  atual; devolve quantas foram encerradas. */
export async function changeMyPassword(
  data: PasswordChangeRequest,
): Promise<PasswordChangeResult> {
  return apiRequest<PasswordChangeResult>("/auth/me/password", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

/** Encerra todas as OUTRAS sessões do usuário, mantendo a atual. */
export async function revokeMyOtherSessions(): Promise<RevokeOtherSessionsResult> {
  return apiRequest<RevokeOtherSessionsResult>("/auth/me/sessions/revoke-others", {
    method: "POST",
  })
}

export async function listUsers() {
  return apiRequest<AppUser[]>("/auth/users", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createUser(data: CreateUserRequest) {
  return apiRequest<AppUser>("/auth/users", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateUser(id: string, data: UpdateUserRequest) {
  return apiRequest<AppUser>(`/auth/users/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteUser(id: string) {
  return apiRequest<void>(`/auth/users/${id}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getPermissionsMatrix() {
  return apiRequest<Record<string, string[]>>("/auth/permissions", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// Search API functions (now SQL via XDR Query API)
export async function runSearch(clientId: number, payload: any) {
  return apiRequest<any>(`/search/${clientId}`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}


export async function waitResults(clientId: number, searchId: string) {
  return apiRequest<any>(`/search/${clientId}/${searchId}/wait`)
}

export async function getSearchStatus(clientId: number, searchId: string) {
  return apiRequest<any>(`/search/${clientId}/${searchId}/status`)
}

export async function fetchResults(clientId: number, searchId: string) {
  return apiRequest<any>(`/search/${clientId}/${searchId}`)
}

// History API functions
export async function listHistory() {
  return apiRequest<any[]>("/history/")
}

export async function listAuditHistory() {
  return apiRequest<AuditHistoryItem[]>("/history/audit")
}

function buildQueryParams(filters?: AuditFilters) {
  const params = new URLSearchParams()

  if (!filters) return params.toString()

  for (const [key, value] of Object.entries(filters)) {
    if (typeof value === "string" && value.trim()) {
      params.set(key, value.trim())
    }
  }

  return params.toString()
}

export async function listAuditHistoryFiltered(filters?: AuditFilters) {
  const params = buildQueryParams(filters)
  return apiRequest<AuditHistoryItem[]>(`/history/audit${params ? `?${params}` : ""}`)
}

export async function downloadAuditHistoryCSV(filters?: AuditFilters) {
  const params = buildQueryParams(filters)
  const response = await fetch(`${BASE_URL}/history/audit/csv${params ? `?${params}` : ""}`, {
    credentials: "include",
  })
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("app-auth-expired"))
  }
  if (!response.ok) throw new Error("Falha ao exportar CSV da auditoria")

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = "audit-history.csv"
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

export async function listSearchHistory(clientId?: number) {
  const params = clientId ? `?client_id=${clientId}` : ""
  return apiRequest<SearchHistoryItem[]>(`/search/history${params}`)
}

export async function getStoredResult(searchId: string) {
  return apiRequest<SearchHistoryItem>(`/search/history/result/${searchId}`)
}

export async function downloadStoredCSV(searchId: string) {
  const response = await fetch(`${BASE_URL}/search/history/result/${searchId}/csv`, {
    credentials: "include",
  })
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("app-auth-expired"))
  }
  if (!response.ok) {
    let errorMessage = "Falha ao baixar CSV"
    try {
      const errorData = await response.json()
      if (typeof errorData?.detail === "string") {
        errorMessage = errorData.detail
      }
    } catch {
      // Ignore JSON parsing errors and keep the fallback message.
    }
    throw new Error(errorMessage)
  }

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = `${searchId}.csv`
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

// Queries API functions
export async function listQueries() {
  return apiRequest<Query[]>("/queries/")
}

export async function createQuery(data: CreateQueryRequest) {
  return apiRequest<Query>("/queries/", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function updateQuery(id: number, data: UpdateQueryRequest) {
  return apiRequest<Query>(`/queries/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
  })
}

export async function deleteQuery(id: number) {
  return apiRequest<void>(`/queries/${id}`, {
    method: "DELETE",
  })
}

export async function getQuery(id: number) {
  return apiRequest<Query>(`/queries/${id}`)
}

// Schedules API functions
export async function listSchedules() {
  return apiRequest<Schedule[]>("/schedules/", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createSchedule(data: CreateScheduleRequest) {
  return apiRequest<Schedule>("/schedules/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteSchedule(id: number) {
  return apiRequest<void>(`/schedules/${id}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getScheduleHistory(scheduleId: number) {
  return apiRequest<SearchHistoryItem[]>(`/schedules/${scheduleId}/history`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// Email API functions
export async function listEmails() {
  return apiRequest<EmailRecipient[]>("/emails/", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createEmail(data: CreateEmailRequest) {
  return apiRequest<EmailRecipient>("/emails/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteEmail(id: number) {
  return apiRequest<void>(`/emails/${id}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getEmailConfig() {
  return apiRequest<EmailConfig>("/emails/config", { forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function updateEmailConfig(data: UpdateEmailConfigRequest) {
  return apiRequest<EmailConfig>("/emails/config", {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function sendTestEmail() {
  return apiRequest<{ detail: string }>("/emails/test", {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Dashboard API ─────────────────────────────────────────────────────

/**
 * GET /dashboard/summary — payload v2 consolidado (fetch ÚNICA do dashboard).
 * O shape v1 (Accept: application/vnd.centralops.v1+json) foi removido junto
 * com a superfície de alertas Wazuh-only.
 */
export async function getDashboardSummary(params?: {
  organization_id?: number | null
  integration_id?: number | null
  platform?: PlatformType | null
  days?: number
}) {
  const searchParams = new URLSearchParams()
  if (params?.organization_id) searchParams.set("organization_id", String(params.organization_id))
  if (params?.integration_id) searchParams.set("integration_id", String(params.integration_id))
  if (params?.platform) searchParams.set("platform", params.platform)
  if (params?.days) searchParams.set("days", String(params.days))
  const qs = searchParams.toString()
  return apiRequest<DashboardSummaryV2>(`/dashboard/summary${qs ? `?${qs}` : ""}`)
}

// ── Organization API ──────────────────────────────────────────────────

export interface ListOrganizationsParams {
  includeInactive?: boolean
  /** Substring case-insensitive em name/slug. */
  name?: string
  /** "active" | "inactive" | "all". Sobrepõe includeInactive. */
  status?: "active" | "inactive" | "all"
  /** "true" | "false" | "all". Default backend: 'all'. */
  autoManaged?: "true" | "false" | "all"
  externalProvider?: string
  /** 1-indexed. Default backend: 1. */
  page?: number
  /** Itens por página. Cap 200. Default backend: 50. */
  size?: number
}

export async function listOrganizations(params: ListOrganizationsParams | boolean = {}) {
  // Compat: chamada antiga `listOrganizations(true)` continua funcionando.
  const opts: ListOrganizationsParams =
    typeof params === "boolean" ? { includeInactive: params } : params

  const search = new URLSearchParams()
  if (opts.includeInactive) search.set("include_inactive", "true")
  if (opts.name && opts.name.trim()) search.set("name", opts.name.trim())
  if (opts.status) search.set("status", opts.status)
  if (opts.autoManaged) search.set("auto_managed", opts.autoManaged)
  if (opts.externalProvider) search.set("external_provider", opts.externalProvider)
  if (opts.page) search.set("page", String(opts.page))
  if (opts.size) search.set("size", String(opts.size))
  const qs = search.toString()
  return apiRequest<Organization[]>(`/organizations/${qs ? `?${qs}` : ""}`)
}

export interface BulkDeactivateOrganizationsResult {
  processed: number
  deactivated: number
  errors: { id: number; reason: string }[]
}

export async function bulkDeactivateOrganizations(ids: number[]) {
  return apiRequest<BulkDeactivateOrganizationsResult>(
    "/organizations/bulk/deactivate",
    {
      method: "POST",
      body: JSON.stringify({ ids }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function createOrganization(data: CreateOrganizationRequest) {
  return apiRequest<Organization>("/organizations/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getOrganization(id: number) {
  return apiRequest<Organization>(`/organizations/${id}`)
}

export async function updateOrganization(id: number, data: UpdateOrganizationRequest) {
  return apiRequest<Organization>(`/organizations/${id}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteOrganization(id: number) {
  return apiRequest<void>(`/organizations/${id}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

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
  return apiRequest<Integration>(`/integrations/${id}`)
}

export async function createIntegration(data: CreateIntegrationRequest) {
  return apiRequest<Integration>("/integrations/", {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateIntegration(id: number, data: UpdateIntegrationRequest) {
  return apiRequest<Integration>(`/integrations/${id}`, {
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
  return apiRequest<DeleteIntegrationResult>(`/integrations/${id}${qs ? `?${qs}` : ""}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Sophos Partner Mode ─────────────────────────────────────────────

export async function syncPartnerTenants(id: number) {
  return apiRequest<PartnerSyncResult>(`/integrations/${id}/sync-tenants`, {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getPartnerSyncStatus(id: number) {
  return apiRequest<PartnerSyncStatus>(`/integrations/${id}/sync-status`)
}

export async function listDiscoveredTenants(id: number, includeInactive = false) {
  const params = new URLSearchParams()
  if (includeInactive) params.set("include_inactive", "true")
  const qs = params.toString()
  return apiRequest<DiscoveredTenant[]>(
    `/integrations/${id}/discovered-tenants${qs ? `?${qs}` : ""}`,
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
    `/integrations/${partnerId}/sophos-tenants${qs ? `?${qs}` : ""}`,
  )
}

export async function selectTenants(
  partnerId: number,
  externalIds: string[],
  state: Extract<TenantSelectionState, "approved" | "excluded">,
) {
  return apiRequest<SophosTenantSelectResponse>(
    `/integrations/${partnerId}/tenants/select`,
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
    `/integrations/${partnerId}/auto-approve-policy`,
    {
      method: "PATCH",
      body: JSON.stringify({ auto_approve_new_tenants: autoApprove }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function testIntegrationConnection(id: number) {
  return apiRequest<TestConnectionResponse>(`/integrations/${id}/test-connection`, {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getIntegrationHealth(id: number) {
  return apiRequest<IntegrationHealth>(`/integrations/${id}/health`, {
    headers: V1_ACCEPT_HEADER,
  })
}

export async function getIntegrationHealthV2(id: number) {
  return apiRequest<HealthResponse>(`/integrations/${id}/health`)
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
    `/providers/${platform}/test-connection`,
    { method: "POST", body: JSON.stringify({ config }) },
  )
}

export async function getIntegrationOverview(id: number) {
  return apiRequest<IntegrationOverview>(`/integrations/${id}/overview`)
}

export async function listSupportedPlatforms() {
  return apiRequest<{ platforms: string[] }>("/integrations/platforms")
}


// ── Collector Multi-Tenant API ────────────────────────────────────────

export async function listCollectorVendors() {
  return apiRequest<CollectorVendor[]>("/collectors/vendors")
}

/**
 * Auto-discovery do mapa `platform → [streams]`.
 *
 * Backend agrega tudo que está registrado no `CollectorRegistry` —
 * adicionar vendor novo via `register()` faz com que ele apareça aqui
 * automaticamente, sem necessidade de editar nenhum hardcode no
 * frontend (BackfillForm, audit panel, etc.).
 */
export async function listPlatformsStreams() {
  return apiRequest<{ platforms: Record<string, string[]> }>(
    "/collectors/platforms-streams",
  )
}

export async function listCollectionState(integrationId?: number) {
  const params = new URLSearchParams()
  if (integrationId) params.set("integration_id", String(integrationId))
  const qs = params.toString()
  return apiRequest<CollectionState[]>(`/collectors/state${qs ? `?${qs}` : ""}`)
}

export async function getCollectorSummary() {
  return apiRequest<CollectorSummary>("/collectors/summary")
}

export async function triggerCollection(integrationId: number, stream: string) {
  return apiRequest<CollectorTriggerResponse>(
    `/collectors/state/${integrationId}/${encodeURIComponent(stream)}/trigger`,
    { method: "POST" },
  )
}

export async function resetCollectorCursor(integrationId: number, stream: string) {
  return apiRequest<void>(
    `/collectors/state/${integrationId}/${encodeURIComponent(stream)}/cursor`,
    { method: "DELETE", forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

// ── Collector Config (runtime settings via UI /config) ──────────────

export async function getCollectorConfig() {
  return apiRequest<CollectorConfig>("/collectors/config", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateCollectorConfig(data: UpdateCollectorConfigRequest) {
  return apiRequest<CollectorConfig>("/collectors/config", {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function testCollectorConfig() {
  return apiRequest<CollectorConfigTestResponse>("/collectors/config/test", {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getCollectorAuditRecent(params?: {
  limit?: number
  platform?: string
  stream?: string
}) {
  const sp = new URLSearchParams()
  if (params?.limit) sp.set("limit", String(params.limit))
  if (params?.platform) sp.set("platform", params.platform)
  if (params?.stream) sp.set("stream", params.stream)
  const qs = sp.toString()
  return apiRequest<CollectorAuditResponse>(
    `/collectors/config/audit/recent${qs ? `?${qs}` : ""}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

export async function clearCollectorAudit() {
  return apiRequest<void>("/collectors/config/audit/recent", {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Edição / licença (open-core) ─────────────────────────────────────

export async function getEdition() {
  return apiRequest<EditionStatus>("/edition")
}

// Ativação de licença: persiste o token assinado CIFRADO no banco, lido
// DB-first pelo resolver de edição. Admin-only no backend.
export async function getLicenseStatus() {
  return apiRequest<LicenseStatus>("/licenses/status")
}

export async function activateLicense(token: string) {
  return apiRequest<LicenseStatus>("/licenses/activate", {
    method: "POST",
    body: JSON.stringify({ token }),
  })
}

export async function deactivateLicense() {
  return apiRequest<LicenseStatus>("/licenses", { method: "DELETE" })
}

/** Total de organizações ATIVAS (lê o header X-Total-Count, que ``apiRequest``
 *  descarta). Usado para a UX de teto de tier (Starter single-tenant). */
export async function countActiveOrganizations(): Promise<number> {
  const res = await fetch(`${BASE_URL}/organizations/?status=active&size=1`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  })
  if (!res.ok) {
    // Espelha apiRequest: 401 dispara o fluxo de sessão expirada (logout/redirect).
    if (res.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("app-auth-expired"))
    }
    throw new ApiRequestError(`HTTP error! status: ${res.status}`, res.status)
  }
  // Guard de NaN: header malformado não deve vazar "NaN / N" para o badge.
  const total = res.headers.get("X-Total-Count")
  const n = total ? Number.parseInt(total, 10) : 0
  return Number.isFinite(n) ? n : 0
}

// ── Captura ao vivo ("listening mode") ────────────────────────────────────────

// Captura ao vivo é POR-TENANT. Admin escopado herda a org implícita (não passa
// nada). Admin global precisa escolher a org: o front passa ?org_id= em TODAS as
// chamadas (o backend aceita org_id em start/list/events/stop/delete). Sem org_id
// explícito, um admin global cairia no guard 400 (org_id obrigatório).
function captureOrgQuery(orgId?: number | null, prefix: "?" | "&" = "?"): string {
  return orgId != null ? `${prefix}org_id=${orgId}` : ""
}

export async function startCaptureSession(
  data: CaptureSessionStartRequest,
  orgId?: number | null,
) {
  return apiRequest<CaptureSession>(
    `/collectors/config/capture-sessions${captureOrgQuery(orgId)}`,
    {
      method: "POST",
      body: JSON.stringify(data),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

export async function listCaptureSessions(orgId?: number | null) {
  return apiRequest<CaptureSessionList>(
    `/collectors/config/capture-sessions${captureOrgQuery(orgId)}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

export async function getCaptureEvents(sessionId: string, limit = 200, orgId?: number | null) {
  return apiRequest<CaptureEventList>(
    `/collectors/config/capture-sessions/${encodeURIComponent(sessionId)}/events?limit=${limit}${captureOrgQuery(orgId, "&")}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

export async function stopCaptureSession(sessionId: string, orgId?: number | null) {
  return apiRequest<void>(
    `/collectors/config/capture-sessions/${encodeURIComponent(sessionId)}/stop${captureOrgQuery(orgId)}`,
    { method: "POST", forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

export async function deleteCaptureSession(sessionId: string, orgId?: number | null) {
  return apiRequest<void>(
    `/collectors/config/capture-sessions/${encodeURIComponent(sessionId)}${captureOrgQuery(orgId)}`,
    { method: "DELETE", forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

// ── Mapping API ──────────────────────────────────────────────────────

import type {
  DriftEntry,
  Mapping,
  MappingAuditEntry,
  MappingPayload,
  MappingRule,
  MappingVersion,
  DryRunResult,
  QuarantineDetail,
  QuarantineEntry,
} from "@/types"

export interface MappingListItem {
  id: string
  vendor: string
  event_type: string
  description?: string | null
  current_version_id: string | null
  rules_count?: number | null
  created_at: string
  updated_at: string
}

export async function listMappings(
  options?: { include_rules_count?: boolean; only_active?: boolean; signal?: AbortSignal },
): Promise<MappingListItem[]> {
  const params = new URLSearchParams()
  if (options?.include_rules_count) {
    params.set("include_rules_count", "true")
  }
  // A política de "só integrações ativas" mora na UI: o default da API é permissivo,
  // então enviamos o valor explícito sempre que definido.
  if (typeof options?.only_active === "boolean") {
    params.set("only_active", String(options.only_active))
  }
  const qs = params.toString()
  return apiRequest<MappingListItem[]>(`/mappings${qs ? `?${qs}` : ""}`, {
    signal: options?.signal,
  })
}

export async function getMapping(id: string, options?: Pick<ApiRequestOptions, "signal">) {
  return apiRequest<Mapping & { versions: MappingVersion[] }>(`/mappings/${id}`, options)
}

export async function getMappingVersions(mappingId: string, options?: Pick<ApiRequestOptions, "signal">) {
  return apiRequest<MappingVersion[]>(`/mappings/${mappingId}/versions`, options)
}

export async function postMappingDryRun(
  payload: {
    /** Lista de regras (interno). Wrap em dict v2 antes de enviar. */
    rules: MappingRule[]
    /** Lista de ops de pré-processamento (default: vazia). */
    preprocess?: import("@/types").PreprocessOp[]
    raw_events?: Record<string, unknown>[]
    vendor?: string
    event_type?: string
    limit?: number
    /** admin global nomeia o tenant cujo reservoir inspecionar. */
    organization_id?: number
  },
  options?: Pick<ApiRequestOptions, "signal">,
) {
  const { rules, preprocess, ...rest } = payload
  const body = {
    ...rest,
    rules: { preprocess: preprocess ?? [], rules },
  }
  return apiRequest<DryRunResult>("/mappings/dry-run", {
    method: "POST",
    body: JSON.stringify(body),
    ...options,
  })
}

/**
 * Resposta paginada do endpoint de audit do backend.
 * Mantida exportada para os hooks que precisarem ler `total` no futuro.
 */
export interface MappingAuditListResponse {
  total: number
  items: MappingAuditEntry[]
  limit: number
  offset: number
}

export async function getMappingAudit(
  id: string,
  params?: {
    limit?: number
    offset?: number
    action?: string
    username?: string
    from_ts?: string
    to_ts?: string
  },
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<MappingAuditEntry[]> {
  const sp = new URLSearchParams()
  if (params?.limit) sp.set("limit", String(params.limit))
  if (params?.offset) sp.set("offset", String(params.offset))
  if (params?.action) sp.set("action", params.action)
  if (params?.username) sp.set("username", params.username)
  if (params?.from_ts) sp.set("from_ts", params.from_ts)
  if (params?.to_ts) sp.set("to_ts", params.to_ts)
  const qs = sp.toString()

  // Backend retorna envelope paginado {total, items, limit, offset}.
  // Defensivo: aceita tanto array direto (caso o backend mude) quanto envelope.
  const response = await apiRequest<MappingAuditListResponse | MappingAuditEntry[]>(
    `/mappings/${id}/audit${qs ? `?${qs}` : ""}`,
    options,
  )
  if (Array.isArray(response)) return response
  return response?.items ?? []
}

// ── Sprint 2: criar versão, rollback, diff ────────────────────────────────────

export interface CreateMappingVersionRequest {
  /** Shape v2: { preprocess, rules }. Backend só aceita esse shape. */
  rules: MappingPayload
  commit_message: string
}

export interface RollbackMappingRequest {
  version_id: string
  commit_message: string
}

export async function createMappingVersion(
  mappingId: string,
  payload: CreateMappingVersionRequest,
) {
  return apiRequest<MappingVersion>(`/mappings/${mappingId}/versions`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function rollbackMapping(
  mappingId: string,
  payload: RollbackMappingRequest,
) {
  return apiRequest<MappingVersion>(`/mappings/${mappingId}/rollback`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export interface MappingVersionDiffResponse {
  definition_id: string
  version_a: string
  version_b: string
  version_a_number: number
  version_b_number: number
  reordered_only: boolean
  added: MappingRule[]
  removed: MappingRule[]
  modified: { target: string; before: MappingRule; after: MappingRule }[]
}

export async function getMappingDiff(
  mappingId: string,
  versionA: string,
  versionB: string,
  options?: Pick<ApiRequestOptions, "signal">,
) {
  return apiRequest<MappingVersionDiffResponse>(
    `/mappings/${mappingId}/versions/${versionA}/diff/${versionB}`,
    options,
  )
}

// ── Sprint 3: Drift API ───────────────────────────────────────────────────────

export interface DriftFiltersParams {
  vendor?: string
  event_type?: string
  status?: "new" | "ignored" | "mapped"
  limit?: number
  offset?: number
}

export interface DriftListResponse {
  items: DriftEntry[]
  total: number
  limit: number
  offset: number
}

export async function listDrift(
  filters?: DriftFiltersParams,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<DriftListResponse> {
  const sp = new URLSearchParams()
  if (filters?.vendor) sp.set("vendor", filters.vendor)
  if (filters?.event_type) sp.set("event_type", filters.event_type)
  if (filters?.status) sp.set("status", filters.status)
  if (filters?.limit != null) sp.set("limit", String(filters.limit))
  if (filters?.offset != null) sp.set("offset", String(filters.offset))
  const qs = sp.toString()
  return apiRequest<DriftListResponse>(`/drift${qs ? `?${qs}` : ""}`, options)
}

export async function ignoreDrift(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<DriftEntry> {
  return apiRequest<DriftEntry>(`/drift/${id}/ignore`, { method: "POST", ...options })
}

export async function markDriftMapped(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<DriftEntry> {
  return apiRequest<DriftEntry>(`/drift/${id}/mark_mapped`, { method: "POST", ...options })
}

export async function deleteDrift(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<void> {
  return apiRequest<void>(`/drift/${id}`, { method: "DELETE", ...options })
}

export interface BulkActionResultItem {
  id: string
  success: boolean
  error?: string | null
}

export interface BulkActionResult {
  updated: number
  failed: number
  items: BulkActionResultItem[]
}

export async function bulkIgnoreDrift(
  field_ids: string[],
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BulkActionResult> {
  return apiRequest<BulkActionResult>("/drift/bulk/ignore", {
    method: "POST",
    body: JSON.stringify({ field_ids }),
    ...options,
  })
}

export async function bulkMarkDriftMapped(
  field_ids: string[],
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BulkActionResult> {
  return apiRequest<BulkActionResult>("/drift/bulk/mark_mapped", {
    method: "POST",
    body: JSON.stringify({ field_ids }),
    ...options,
  })
}

/**
 * GET /mappings/normalize/type-casts
 * Retorna a lista dinâmica de funções de cast registradas no backend, ordenada
 * alfabeticamente pelo name. Usado pelo useTypeCasts hook para popular o
 * dropdown de type_cast no RuleRow sem hardcode no frontend.
 */
export async function fetchTypeCasts(
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<TypeCastDescriptor[]> {
  return apiRequest<TypeCastDescriptor[]>("/mappings/normalize/type-casts", options)
}

/**
 * Campos descobertos pelo drift detector para um mapping.
 *
 * Backend agrega UnknownField por (vendor, event_type) e retorna ordenado
 * por occurrences DESC, limit 100, com Cache-Control: private, max-age=60.
 */
export interface DiscoveredField {
  path: string
  occurrences: number
  sample_values: string[]
  first_seen_at: string
}

export interface DiscoverFieldsResponse {
  fields: DiscoveredField[]
}

/**
 * GET /mappings/{id}/discover-fields — alimenta o autocomplete de JMESPath
 * no editor de regras. Se o drift ainda não tiver eventos coletados, o
 * backend retorna { fields: [] } e a UI cai para input texto livre.
 */
export async function getDiscoveredFields(
  mappingId: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<DiscoverFieldsResponse> {
  return apiRequest<DiscoverFieldsResponse>(`/mappings/${mappingId}/discover-fields`, options)
}

// ── Sprint 3: Quarantine API ──────────────────────────────────────────────────

export type QuarantineStatusFilter = "pending" | "reprocessed" | "all"

export interface QuarantineFiltersParams {
  vendor?: string
  event_type?: string
  error_kind?: string
  integration_id?: number
  /** substring case-insensitive sobre Integration.name. */
  integration_name?: string
  /** filtra por reprocessed_at (default backend = "pending"). */
  status?: QuarantineStatusFilter
  limit?: number
  offset?: number
}

export interface QuarantineListResponse {
  items: QuarantineEntry[]
  total: number
  limit: number
  offset: number
}

export async function listQuarantine(
  filters?: QuarantineFiltersParams,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineListResponse> {
  const sp = new URLSearchParams()
  if (filters?.vendor) sp.set("vendor", filters.vendor)
  if (filters?.event_type) sp.set("event_type", filters.event_type)
  if (filters?.error_kind) sp.set("error_kind", filters.error_kind)
  if (filters?.integration_id != null) sp.set("integration_id", String(filters.integration_id))
  if (filters?.integration_name) sp.set("integration_name", filters.integration_name)
  if (filters?.status) sp.set("status", filters.status)
  if (filters?.limit != null) sp.set("limit", String(filters.limit))
  if (filters?.offset != null) sp.set("offset", String(filters.offset))
  const qs = sp.toString()
  return apiRequest<QuarantineListResponse>(`/quarantine${qs ? `?${qs}` : ""}`, options)
}

export async function getQuarantineDetail(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineDetail> {
  return apiRequest<QuarantineDetail>(`/quarantine/${id}`, options)
}

export async function discardQuarantine(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<void> {
  return apiRequest<void>(`/quarantine/${id}/discard`, { method: "POST", ...options })
}

export async function reprocessQuarantine(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineEntry> {
  return apiRequest<QuarantineEntry>(`/quarantine/${id}/reprocess`, { method: "POST", ...options })
}

// ── bulk operations + select-all-filter ────────────────────────

export interface QuarantineBulkErrorItem {
  id: string
  reason: string
}

export interface QuarantineBulkDiscardResponse {
  processed: number
  discarded: number
  errors: QuarantineBulkErrorItem[]
}

export interface QuarantineBulkReprocessResponse {
  accepted: number
  expired: number
  already_reprocessed: number
  errors: QuarantineBulkErrorItem[]
}

export interface QuarantineBulkIdsResponse {
  total: number
  ids: string[]
  capped: boolean
}

/** Cap operacional: backend rejeita >500 IDs/request. Frontend pagina
 *  internamente em batches se a seleção exceder. */
export const QUARANTINE_BULK_BATCH_SIZE = 500

/** Cap absoluto do "selecionar tudo do filtro" (alinhado com backend). */
export const QUARANTINE_BULK_IDS_MAX = 2000

export async function bulkDiscardQuarantine(
  ids: string[],
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineBulkDiscardResponse> {
  return apiRequest<QuarantineBulkDiscardResponse>("/quarantine/bulk/discard", {
    method: "POST",
    body: JSON.stringify({ ids }),
    ...options,
  })
}

export async function bulkReprocessQuarantine(
  ids: string[],
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineBulkReprocessResponse> {
  return apiRequest<QuarantineBulkReprocessResponse>("/quarantine/bulk/reprocess", {
    method: "POST",
    body: JSON.stringify({ ids }),
    ...options,
  })
}

/**
 * GET /quarantine/bulk/ids — IDs casados pelos filtros (cap ``max`` ≤ 2000).
 * Usado pelo botão "Selecionar tudo do filtro": payload mais leve que
 * paginar a lista completa só para extrair IDs.
 */
export async function listQuarantineIds(
  filters?: QuarantineFiltersParams,
  max?: number,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineBulkIdsResponse> {
  const sp = new URLSearchParams()
  if (filters?.vendor) sp.set("vendor", filters.vendor)
  if (filters?.event_type) sp.set("event_type", filters.event_type)
  if (filters?.error_kind) sp.set("error_kind", filters.error_kind)
  if (filters?.integration_id != null) sp.set("integration_id", String(filters.integration_id))
  if (filters?.integration_name) sp.set("integration_name", filters.integration_name)
  if (filters?.status) sp.set("status", filters.status)
  if (max != null) sp.set("max", String(max))
  const qs = sp.toString()
  return apiRequest<QuarantineBulkIdsResponse>(
    `/quarantine/bulk/ids${qs ? `?${qs}` : ""}`,
    options,
  )
}

// ── Sprint 5: Pipeline Health API ─────────────────────────────────────────────

import type { IntegrationPipelineHealth } from "@/types"

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
    `/integrations/${integrationId}/pipeline-health`,
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

// ── Sprint 2: Backfill API ─────────────────────────────────────────────

import type { BackfillJob, BackfillJobStatus, CreateBackfillJobRequest } from "@/types"

export async function listBackfillJobs(
  integrationId: number,
  filters?: { limit?: number; offset?: number; status?: BackfillJobStatus },
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<{ items: BackfillJob[]; total: number; limit: number; offset: number }> {
  const sp = new URLSearchParams()
  if (filters?.limit != null) sp.set("limit", String(filters.limit))
  if (filters?.offset != null) sp.set("offset", String(filters.offset))
  if (filters?.status) sp.set("status", filters.status)
  const qs = sp.toString()
  return apiRequest<{ items: BackfillJob[]; total: number; limit: number; offset: number }>(
    `/integrations/${integrationId}/backfill-jobs${qs ? `?${qs}` : ""}`,
    options,
  )
}

export async function getBackfillJob(
  jobId: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BackfillJob> {
  return apiRequest<BackfillJob>(`/backfill-jobs/${jobId}`, options)
}

export async function createBackfillJob(
  integrationId: number,
  payload: CreateBackfillJobRequest,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BackfillJob> {
  return apiRequest<BackfillJob>(`/integrations/${integrationId}/backfill`, {
    method: "POST",
    body: JSON.stringify(payload),
    ...options,
  })
}

export async function cancelBackfillJob(
  jobId: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BackfillJob> {
  return apiRequest<BackfillJob>(`/backfill-jobs/${jobId}/cancel`, {
    method: "POST",
    ...options,
  })
}

// ── Personal Access Tokens + Service Accounts ──────

import type {
  ApiToken,
  ApiTokenCreateRequest,
  ApiTokenCreateResponse,
  ScopeName,
  ServiceAccount,
  ServiceAccountCreateRequest,
  ServiceAccountUpdateRequest,
} from "@/types"

export async function listApiTokens(
  options?: { include_revoked?: boolean },
): Promise<ApiToken[]> {
  const qs = options?.include_revoked ? "?include_revoked=true" : ""
  return apiRequest<ApiToken[]>(`/v1/tokens${qs}`)
}

export async function createApiToken(
  payload: ApiTokenCreateRequest,
): Promise<ApiTokenCreateResponse> {
  return apiRequest<ApiTokenCreateResponse>("/v1/tokens", {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function revokeApiToken(tokenId: number): Promise<void> {
  return apiRequest<void>(`/v1/tokens/${tokenId}`, {
    method: "DELETE",
  })
}

/** Lista de scopes válidos (= Permission enum no backend). Cacheável.  */
export async function listScopes(): Promise<ScopeName[]> {
  return apiRequest<ScopeName[]>("/v1/tokens/scopes")
}

// ── Service Accounts (admin only) ────────────────────────────────────

const SA_BASE = "/v1/service-accounts"

export async function listServiceAccounts(
  options?: { include_inactive?: boolean },
): Promise<ServiceAccount[]> {
  const qs = options?.include_inactive ? "?include_inactive=true" : ""
  return apiRequest<ServiceAccount[]>(`${SA_BASE}${qs}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getServiceAccount(saId: number): Promise<ServiceAccount> {
  return apiRequest<ServiceAccount>(`${SA_BASE}/${saId}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function createServiceAccount(
  payload: ServiceAccountCreateRequest,
): Promise<ServiceAccount> {
  return apiRequest<ServiceAccount>(SA_BASE, {
    method: "POST",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateServiceAccount(
  saId: number,
  payload: ServiceAccountUpdateRequest,
): Promise<ServiceAccount> {
  return apiRequest<ServiceAccount>(`${SA_BASE}/${saId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteServiceAccount(saId: number): Promise<void> {
  return apiRequest<void>(`${SA_BASE}/${saId}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function listServiceAccountTokens(
  saId: number,
  options?: { include_revoked?: boolean },
): Promise<ApiToken[]> {
  const qs = options?.include_revoked ? "?include_revoked=true" : ""
  return apiRequest<ApiToken[]>(`${SA_BASE}/${saId}/tokens${qs}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function createServiceAccountToken(
  saId: number,
  payload: ApiTokenCreateRequest,
): Promise<ApiTokenCreateResponse> {
  return apiRequest<ApiTokenCreateResponse>(`${SA_BASE}/${saId}/tokens`, {
    method: "POST",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function revokeServiceAccountToken(
  saId: number,
  tokenId: number,
): Promise<void> {
  return apiRequest<void>(`${SA_BASE}/${saId}/tokens/${tokenId}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Destinos (saída multi-destino) ───────────────────────────

import type {
  Destination,
  DestinationCreateRequest,
  DestinationHealth,
  DestinationShadowResult,
  DestinationTestResult,
  DestinationType,
  DestinationUpdateRequest,
} from "@/types"

const DEST_BASE = "/collectors/destinations"

export async function listDestinations(
  params?: { include_disabled?: boolean; org_id?: number; offset?: number; limit?: number },
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<Destination[]> {
  const qs = new URLSearchParams()
  if (params?.include_disabled) qs.set("include_disabled", "true")
  if (params?.org_id != null) qs.set("org_id", String(params.org_id))
  if (params?.offset != null) qs.set("offset", String(params.offset))
  if (params?.limit != null) qs.set("limit", String(params.limit))
  const q = qs.toString()
  return apiRequest<Destination[]>(`${DEST_BASE}${q ? `?${q}` : ""}`, {
    ...options,
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getDestination(id: string): Promise<Destination> {
  return apiRequest<Destination>(`${DEST_BASE}/${encodeURIComponent(id)}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function createDestination(
  data: DestinationCreateRequest,
): Promise<Destination> {
  return apiRequest<Destination>(DEST_BASE, {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateDestination(
  id: string,
  data: DestinationUpdateRequest,
): Promise<Destination> {
  return apiRequest<Destination>(`${DEST_BASE}/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteDestination(id: string): Promise<void> {
  return apiRequest<void>(`${DEST_BASE}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function testDestination(id: string): Promise<DestinationTestResult> {
  return apiRequest<DestinationTestResult>(`${DEST_BASE}/${encodeURIComponent(id)}/test`, {
    method: "POST",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function shadowDestination(
  id: string,
  sample?: Record<string, unknown> | null,
): Promise<DestinationShadowResult> {
  return apiRequest<DestinationShadowResult>(`${DEST_BASE}/${encodeURIComponent(id)}/shadow`, {
    method: "POST",
    body: JSON.stringify({ sample: sample ?? null }),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getDestinationHealth(id: string): Promise<DestinationHealth> {
  return apiRequest<DestinationHealth>(`${DEST_BASE}/${encodeURIComponent(id)}/health`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

/**
 * GET /collectors/destinations/health
 * Saúde de TODOS os destinos da org em uma chamada.
 * Usado para badges de status na lista principal sem N chamadas por card.
 */
export async function listDestinationsHealth(
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<import("@/types").DestinationHealthBatchResponse> {
  return apiRequest(`${DEST_BASE}/health`, {
    ...options,
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getDestinationDlq(
  id: string,
  params?: { offset?: number; limit?: number },
): Promise<import("@/types").DestinationDlqResponse> {
  const qs = new URLSearchParams()
  if (params?.offset != null) qs.set("offset", String(params.offset))
  if (params?.limit != null) qs.set("limit", String(params.limit))
  const q = qs.toString()
  return apiRequest(`${DEST_BASE}/${encodeURIComponent(id)}/dlq${q ? `?${q}` : ""}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getDestinationTap(
  id: string,
  params?: { limit?: number },
): Promise<import("@/types").DestinationTap> {
  const qs = new URLSearchParams()
  if (params?.limit != null) qs.set("limit", String(params.limit))
  const q = qs.toString()
  return apiRequest(`${DEST_BASE}/${encodeURIComponent(id)}/tap${q ? `?${q}` : ""}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getDestinationMetrics(
  id: string,
  params?: { range_minutes?: number; step_seconds?: number },
): Promise<import("@/types").DestinationMetrics> {
  const qs = new URLSearchParams()
  if (params?.range_minutes != null) qs.set("range_minutes", String(params.range_minutes))
  if (params?.step_seconds != null) qs.set("step_seconds", String(params.step_seconds))
  const q = qs.toString()
  return apiRequest(`${DEST_BASE}/${encodeURIComponent(id)}/metrics${q ? `?${q}` : ""}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function listDestinationTypes(): Promise<DestinationType[]> {
  return apiRequest<DestinationType[]>(`${DEST_BASE}/destination-types`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ── Ingestão push (FortiGate/WEC) ────────────────────────────
export interface IngestInfo {
  integration_id: number
  platform: string
  transport: string
  streams: string[]
  has_token: boolean
  endpoint_base: string
  buffer_depth: number
  icon_id?: string | null
}

/** Metadados de ingestão. Lança (422) se a integração não é uma fonte push. */
export async function getIngestInfo(integrationId: number): Promise<IngestInfo> {
  return apiRequest<IngestInfo>(`/ingest/integrations/${integrationId}`)
}

/** Emite/rotaciona o token de ingestão. Devolve o token em claro UMA vez. */
export async function issueIngestToken(integrationId: number): Promise<{ token: string; endpoint: string }> {
  return apiRequest<{ token: string; endpoint: string }>(`/ingest/integrations/${integrationId}/token`, {
    method: "POST",
  })
}

/** Revoga o token de ingestão SEM rotacionar (mata um token vazado). 204/404. */
export async function revokeIngestToken(integrationId: number): Promise<void> {
  await apiRequest<void>(`/ingest/integrations/${integrationId}/token`, {
    method: "DELETE",
  })
}

// ── resumo de volume/redução/custo ────────────────────────
export interface CostSummaryRow {
  organization_id: number
  bytes_in: number
  bytes_out: number
  events_in: number
  events_out: number
  out_in_byte_ratio: number | null
  reduction_active: boolean
  bytes_saved: number
  reduction_pct: number | null
  savings_usd_per_day: number | null
  cost: { usd: number; currency: string } | null
}
export interface CostSummary {
  window_minutes: number
  enabled: boolean
  pricing_available: boolean
  rows: CostSummaryRow[]
  note: string
}

/** Volume ingerido vs entregue + economia por-org. O bloco US$ só vem quando
 *  o pacote Enterprise registra um pricer (Community devolve só volume + % de redução). */
export async function getCostSummary(): Promise<CostSummary> {
  return apiRequest<CostSummary>(`/collectors/cost-summary`)
}

// ── Rotas (motor de roteamento) ───────────────────────

import type {
  Route,
  RouteAudit,
  RouteCreateRequest,
  RouteDryRunRequest,
  RouteDryRunResponse,
  RouteUpdateRequest,
} from "@/types"

const ROUTES_BASE = "/collectors/routes"

export async function listRoutes(
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<Route[]> {
  return apiRequest<Route[]>(ROUTES_BASE, { ...options, forbiddenRedirectTo: ADMIN_REDIRECT_PATH })
}

export async function createRoute(data: RouteCreateRequest): Promise<Route> {
  return apiRequest<Route>(ROUTES_BASE, {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateRoute(id: string, data: RouteUpdateRequest): Promise<Route> {
  return apiRequest<Route>(`${ROUTES_BASE}/${encodeURIComponent(id)}`, {
    method: "PUT",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteRoute(id: string): Promise<void> {
  return apiRequest<void>(`${ROUTES_BASE}/${encodeURIComponent(id)}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function dryRunRoutes(data: RouteDryRunRequest): Promise<RouteDryRunResponse> {
  return apiRequest<RouteDryRunResponse>(`${ROUTES_BASE}/dry-run`, {
    method: "POST",
    body: JSON.stringify(data),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function rollbackRoute(id: string, auditId: string): Promise<Route> {
  return apiRequest<Route>(`${ROUTES_BASE}/${encodeURIComponent(id)}/rollback`, {
    method: "POST",
    body: JSON.stringify({ audit_id: auditId }),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function routeAudit(id: string): Promise<RouteAudit[]> {
  return apiRequest<RouteAudit[]>(`${ROUTES_BASE}/${encodeURIComponent(id)}/audit`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getRouteMetrics(
  id: string,
  params?: { range_minutes?: number },
): Promise<import("@/types").RouteMetrics> {
  const qs = new URLSearchParams()
  if (params?.range_minutes != null) qs.set("range_minutes", String(params.range_minutes))
  const q = qs.toString()
  return apiRequest(`${ROUTES_BASE}/${encodeURIComponent(id)}/metrics${q ? `?${q}` : ""}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

/**
 * GET /collectors/routes/topology
 * Topologia do fluxo de roteamento com throughput por rota/destino
 * (flow-view com throughput).
 */
export async function getRoutingTopology(
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<import("@/types").RoutingTopologyResponse> {
  return apiRequest(`${ROUTES_BASE}/topology`, {
    ...options,
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getFlowGraph(
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<import("@/types").FlowGraph> {
  return apiRequest(`${ROUTES_BASE}/flow`, {
    ...options,
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}


// ── novos endpoints ───────────────────────

import type {
  RouteReorderResponse,
  DlqReprocessResponse,
  CredentialRotateRequest,
  CredentialRotateResponse,
  CredentialRevokeResponse,
  CredentialAuditResponse,
  DestinationLineageResponse,
  EventLineageResponse,
  ConfigBundle,
  ConfigImportRequest,
  ConfigImportResponse,
} from "@/types"

/**
 * POST /collectors/routes/reorder
 * Reatribui prioridades em bulk na ordem fornecida (drag-and-drop).
 * Requer ROUTING_ENABLED=true no backend (503 caso contrário).
 */
export async function reorderRoutes(routeIds: string[]): Promise<RouteReorderResponse> {
  return apiRequest<RouteReorderResponse>(`${ROUTES_BASE}/reorder`, {
    method: "POST",
    body: JSON.stringify({ route_ids: routeIds }),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

/**
 * POST /collectors/destinations/{id}/dlq/reprocess
 * Re-enfileira entradas mortas para reentrega.
 * eventIds omitido → drena TODO o DLQ do destino.
 * Requer MULTI_DESTINATION_ENABLED=true (503 caso contrário).
 */
export async function reprocessDestinationDlq(
  id: string,
  eventIds?: string[] | null,
): Promise<DlqReprocessResponse> {
  return apiRequest<DlqReprocessResponse>(
    `${DEST_BASE}/${encodeURIComponent(id)}/dlq/reprocess`,
    {
      method: "POST",
      body: JSON.stringify({ event_ids: eventIds ?? null }),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

/**
 * POST /collectors/destinations/{id}/credential/rotate
 * Rotaciona a credencial do destino.
 * Requer MULTI_DESTINATION_ENABLED=true (503 caso contrário).
 */
export async function rotateCredential(
  id: string,
  body: CredentialRotateRequest,
): Promise<CredentialRotateResponse> {
  return apiRequest<CredentialRotateResponse>(
    `${DEST_BASE}/${encodeURIComponent(id)}/credential/rotate`,
    {
      method: "POST",
      body: JSON.stringify(body),
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

/**
 * POST /collectors/destinations/{id}/credential/revoke
 * Revoga a credencial do destino e desabilita-o.
 * Requer MULTI_DESTINATION_ENABLED=true (503 caso contrário).
 */
export async function revokeCredential(id: string): Promise<CredentialRevokeResponse> {
  return apiRequest<CredentialRevokeResponse>(
    `${DEST_BASE}/${encodeURIComponent(id)}/credential/revoke`,
    {
      method: "POST",
      forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
    },
  )
}

/**
 * GET /collectors/destinations/{id}/credential/audit
 * Trilha de auditoria de acesso à credencial.
 * Requer MULTI_DESTINATION_ENABLED=true (503 caso contrário).
 */
export async function getCredentialAudit(
  id: string,
  params?: { offset?: number; limit?: number },
): Promise<CredentialAuditResponse> {
  const qs = new URLSearchParams()
  if (params?.offset != null) qs.set("offset", String(params.offset))
  if (params?.limit != null) qs.set("limit", String(params.limit))
  const q = qs.toString()
  return apiRequest<CredentialAuditResponse>(
    `${DEST_BASE}/${encodeURIComponent(id)}/credential/audit${q ? `?${q}` : ""}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

/**
 * GET /collectors/destinations/{id}/lineage?event_id=...
 * Lineage de um evento específico neste destino.
 */
export async function getDestinationLineage(
  id: string,
  eventId: string,
): Promise<DestinationLineageResponse> {
  const qs = new URLSearchParams({ event_id: eventId })
  return apiRequest<DestinationLineageResponse>(
    `${DEST_BASE}/${encodeURIComponent(id)}/lineage?${qs.toString()}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

/**
 * GET /collectors/lineage/{event_id}
 * Lineage de um evento em todos os destinos da org (admin, org-scoped).
 */
export async function getEventLineage(
  eventId: string,
  params?: { org_id?: number },
): Promise<EventLineageResponse> {
  const qs = new URLSearchParams()
  if (params?.org_id != null) qs.set("org_id", String(params.org_id))
  const q = qs.toString()
  return apiRequest<EventLineageResponse>(
    `/collectors/lineage/${encodeURIComponent(eventId)}${q ? `?${q}` : ""}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

// ── Config-as-code (GitOps) ──────────────────────────────

const CONFIG_BASE = "/collectors/config"

/**
 * GET /collectors/config/export
 * Exporta destinos + rotas da org como bundle versionado (sem credenciais).
 * Requer MULTI_DESTINATION_ENABLED=true (503 caso contrário).
 */
export async function exportConfigBundle(): Promise<ConfigBundle> {
  return apiRequest<ConfigBundle>(`${CONFIG_BASE}/export`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

/**
 * POST /collectors/config/import
 * Aplica (ou simula com dry_run=true) um bundle no banco de dados.
 * Idempotente: match por nome dentro da org.
 * Requer MULTI_DESTINATION_ENABLED=true (503 caso contrário).
 */
export async function importConfigBundle(
  bundle: ConfigBundle,
  options?: { dryRun?: boolean; secrets?: Record<string, string> },
): Promise<ConfigImportResponse> {
  const payload: ConfigImportRequest = {
    bundle,
    dry_run: options?.dryRun ?? true,
    ...(options?.secrets ? { secrets: options.secrets } : {}),
  }
  return apiRequest<ConfigImportResponse>(`${CONFIG_BASE}/import`, {
    method: "POST",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

// ─────────────────────────────────────────────────────────────────────────────
// Plano de Querys (busca federada, jobs async, detecções, correlação)
// ─────────────────────────────────────────────────────────────────────────────

const QUERY_JOBS_BASE = "/query-jobs"
const DETECTIONS_BASE = "/detections"
const CORRELATION_RULES_BASE = "/correlation-rules"

/** Catálogo de dialetos de query suportados (só exige autenticação). */
export async function listQueryCapabilities() {
  return apiRequest<QueryCapabilityRead[]>("/providers/query-capabilities")
}

/** Submete um job de query federada (202 → job com status submitted). QUERY_RUN. */
export async function submitQueryJob(data: QueryJobSubmitRequest) {
  return apiRequest<QueryJobRead>(QUERY_JOBS_BASE, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

/** Poll do estado de um job. `signal` permite abortar o polling no unmount. */
export async function getQueryJob(jobId: string, signal?: AbortSignal) {
  return apiRequest<QueryJobRead>(`${QUERY_JOBS_BASE}/${encodeURIComponent(jobId)}`, { signal })
}

/** Lista jobs recentes org-scoped (limit padrão 50). */
export async function listQueryJobs(limit = 50) {
  const params = new URLSearchParams({ limit: String(limit) })
  return apiRequest<QueryJobRead[]>(`${QUERY_JOBS_BASE}?${params}`)
}

/** Lista detecções org-scoped (filtro opcional por status). */
export async function listDetections(params?: { status_filter?: DetectionStatusUpdate["status"]; limit?: number }) {
  const q = new URLSearchParams()
  if (params?.status_filter) q.set("status_filter", params.status_filter)
  if (params?.limit) q.set("limit", String(params.limit))
  const qs = q.toString()
  return apiRequest<DetectionRead[]>(`${DETECTIONS_BASE}${qs ? `?${qs}` : ""}`)
}

export async function getDetection(id: number) {
  return apiRequest<DetectionRead>(`${DETECTIONS_BASE}/${id}`)
}

/** Triagem: muda o status de uma detecção (open→ack→closed). QUERY_RUN. */
export async function updateDetectionStatus(id: number, payload: DetectionStatusUpdate) {
  return apiRequest<DetectionRead>(`${DETECTIONS_BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  })
}

/** Lista regras de correlação org-scoped. QUERY_RUN. */
export async function listCorrelationRules() {
  return apiRequest<CorrelationRuleRead[]>(`${CORRELATION_RULES_BASE}`)
}

export async function getCorrelationRule(id: number) {
  return apiRequest<CorrelationRuleRead>(`${CORRELATION_RULES_BASE}/${id}`)
}

/** Cria regra de correlação (201). QUERY_SAVE. 409 = quota por org estourada. */
export async function createCorrelationRule(data: CorrelationRuleCreate) {
  return apiRequest<CorrelationRuleRead>(`${CORRELATION_RULES_BASE}`, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function updateCorrelationRule(id: number, data: CorrelationRuleUpdate) {
  return apiRequest<CorrelationRuleRead>(`${CORRELATION_RULES_BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  })
}

export async function deleteCorrelationRule(id: number) {
  return apiRequest<void>(`${CORRELATION_RULES_BASE}/${id}`, {
    method: "DELETE",
  })
}

// ── OCSF governance — admin ────────────────────────────────────
export async function listOcsfPolicies() {
  return apiRequest<OcsfPolicy[]>("/ocsf/policies", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function setOcsfPolicy(orgId: number, enforcementMode: OcsfEnforcementMode) {
  return apiRequest<OcsfPolicy>(`/ocsf/policies/${orgId}`, {
    method: "PUT",
    body: JSON.stringify({ enforcement_mode: enforcementMode }),
  })
}

export async function getOcsfCompliance() {
  return apiRequest<OcsfCompliance>("/ocsf/compliance", {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}
