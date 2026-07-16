import type React from "react"

// Catálogo de plataformas é 100% plugin-driven (GET /providers/platforms é a
// fonte da verdade). Variantes-card como `sophos_partner`/`sophos_organization`
// e vendors novos entram sem editar este tipo. Por isso `string`
// em vez de uma union fechada — que ficava desatualizada e exigia cast mentiroso.
export type PlatformType = string

/**
 * Dialetos de query suportados pela plataforma. Cada fonte declara
 * o seu via QueryCapability; o catálogo vem de GET /providers/query-capabilities.
 */
export type QueryDialect = "opensearch_dsl" | "xdr_data_lake" | "fql" | "kql" | "lake_filter"

/** Modo de tradução do statement submetido. */
export type QuerySpecKind = "passthrough" | "sigma"
// backend já retorna os 4 papéis
export type UserRole = "viewer" | "operator" | "engineer" | "admin"
export type ScheduleTimeUnit = "minutes" | "hours" | "days" | "weeks"

// Sophos Partner Mode hierarchy. ``tenant`` is the legacy default (1 cred per
// integration). ``partner``/``organization`` unlock auto-discovery of children.
export type IntegrationKind = "partner" | "organization" | "tenant"
// "enterprise_required": backend Community (sem os módulos EE) recusou o sync.
// "license_required": módulos EE presentes, mas a licença não cobre a feature.
export type TenantSyncStatus =
  | "ok"
  | "partial"
  | "error"
  | "enterprise_required"
  | "license_required"
export type DiscoveredTenantStatus = "new" | "linked" | "stale"

/**
 * Estado de seleção de um tenant Partner (opt-in selection).
 * - `pending`   : descoberto mas ainda sem decisão (não materializa coleta).
 * - `approved`  : aprovado, child Integration ativa + ScheduleConfigs criados.
 * - `excluded`  : excluído (soft-delete; child desativada, histórico preservado).
 * - `stale`     : sumiu do último sync da Sophos; aguarda decisão.
 */
export type TenantSelectionState = "pending" | "approved" | "excluded" | "stale"

export interface Organization {
  id: number
  name: string
  slug: string
  description?: string
  is_active: boolean
  integration_count: number
  // Sophos Partner Mode auto-onboarding fields.
  external_provider?: string | null
  external_id?: string | null
  auto_managed?: boolean
  iris_customer_id?: number | null
  partner_integration_id?: number | null
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateOrganizationRequest {
  name: string
  slug?: string
  description?: string
}

export interface UpdateOrganizationRequest {
  name?: string
  description?: string
  is_active?: boolean
}

export interface Integration {
  id: number
  organization_id: number
  organization_name?: string
  name: string
  platform: PlatformType
  is_active: boolean
  is_authenticated: boolean
  auth_status: "healthy" | "degraded" | "error" | "unknown"
  last_checked_at?: string | null
  last_successful_check_at?: string | null
  last_error?: string
  // Sophos Partner Mode hierarchy + sync metadata.
  kind?: IntegrationKind
  parent_integration_id?: number | null
  external_id?: string | null
  id_type?: string | null
  data_geography?: string | null
  last_tenant_sync_at?: string | null
  tenant_sync_status?: TenantSyncStatus | null
  auto_managed?: boolean
  /** Partner-only: quando true, novos tenants descobertos são auto-aprovados. */
  auto_approve_new_tenants?: boolean
  /** Populated only for kind="partner"/"organization" listings. */
  children_count?: number | null
  client_id?: string
  region?: string
  tenant_id?: string
  manager_url?: string
  indexer_url?: string
  manager_api_username?: string
  indexer_username?: string
  manager_api_password_configured?: boolean
  indexer_password_configured?: boolean
  verify_ssl?: boolean
  capabilities: string[]
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateIntegrationRequest {
  organization_id: number
  name: string
  platform: PlatformType
  /** Defaults to "tenant" (legacy single-cred). Set "partner"/"organization" to
   *  bootstrap auto-discovery of child tenants under one Sophos credential. */
  kind?: IntegrationKind
  client_id?: string
  client_secret?: string
  region?: string
  manager_url?: string
  indexer_url?: string
  manager_api_username?: string
  manager_api_password?: string
  indexer_username?: string
  indexer_password?: string
  verify_ssl?: boolean
}

// ── Sophos Partner Mode — sync & discovery payloads ─────────────────

export interface DiscoveredTenant {
  external_id: string
  name: string
  region?: string | null
  data_geography?: string | null
  api_host?: string | null
  /** Legacy status (sync-tenants endpoint). New tenant-selection endpoint
   *  populates `selection_state` instead — UI prefers it when present. */
  status: DiscoveredTenantStatus
  linked_organization_id?: number | null
  linked_integration_id?: number | null
}

// ── Sophos Partner Mode — Tenant selection ───────────────────

/**
 * Snapshot Sophos por tenant + estado de seleção. Devolvido por
 * GET /integrations/{partner_id}/sophos-tenants.
 */
export interface SophosTenantItem {
  external_id: string
  name: string
  region?: string | null
  data_geography?: string | null
  api_host?: string | null
  selection_state: TenantSelectionState
  child_integration_id?: number | null
  decided_by?: string | null
  decided_at?: string | null
  last_seen_at: string
}

export interface SophosTenantListResponse {
  items: SophosTenantItem[]
  total: number
  page: number
  size: number
  /** True quando `?refresh=true` forçou chamada Sophos `/partner/v1/tenants`. */
  fetched_live: boolean
  auto_approve_new_tenants: boolean
  last_tenant_sync_at?: string | null
  tenant_sync_status?: string | null
}

export interface SophosTenantSelectRequest {
  external_ids: string[]
  state: Extract<TenantSelectionState, "approved" | "excluded">
}

export interface SophosTenantSelectResponse {
  processed: number
  materialized: number
  deactivated: number
  pending: number
  errors: Array<{ external_id: string; reason: string }>
  /** True quando o backend Community (sem módulos EE) persistiu as decisões
   *  mas NÃO materializou/desativou nada. */
  enterprise_required?: boolean
  /** True quando os módulos EE existem mas a licença ativa não cobre a
   *  feature (ausente/expirada pós-grace/plano sem multi_tenant). */
  license_required?: boolean
}

export interface AutoApprovePolicyResponse {
  integration_id: number
  auto_approve_new_tenants: boolean
  updated_at: string
}

export interface PartnerSyncResult {
  integration_id: number
  discovered: number
  created: number
  linked: number
  deactivated: number
  errors: string[]
  started_at: string
  finished_at?: string | null
  status: TenantSyncStatus
}

export interface PartnerSyncStatus {
  integration_id: number
  tenant_sync_status?: TenantSyncStatus | null
  last_tenant_sync_at?: string | null
  lock_active: boolean
}

export interface UpdateIntegrationRequest {
  name?: string
  is_active?: boolean
  client_id?: string
  client_secret?: string | null
  region?: string | null
  // Manager é opcional no Wazuh — `null` revoga as credenciais (Indexer-only).
  manager_url?: string | null
  indexer_url?: string | null
  manager_api_username?: string | null
  manager_api_password?: string | null
  indexer_username?: string | null
  indexer_password?: string | null
  verify_ssl?: boolean
}

export interface IntegrationHealth {
  integration_id: number
  status: string
  details: Record<string, any>
  checked_at?: string | null
  manager_status?: string | null
  indexer_status?: string | null
}

// ── Health v2 ─────────────────────────────────────────────────────

export type HealthSeverity = "ok" | "warn" | "critical" | "unknown"

export interface HealthMetric {
  id: string
  label: string
  value: string | number | boolean
  unit?: string | null
  severity: HealthSeverity
  icon_id?: string | null
  hint?: string | null
  group?: string | null
}

export interface HealthResponse {
  schema_version: 2
  platform: string
  last_collection_at?: string | null
  last_success_at?: string | null
  metrics: HealthMetric[]
}

// ── Dashboard v2 ──────────────────────────────────────────────────

export type DashSeverity = "ok" | "warn" | "critical" | "info"

export interface KpiCard {
  id: string
  label: string
  value: string | number
  sub?: string | null
  icon_id?: string | null
  trend?: "up" | "down" | "flat" | null
  trend_value?: string | null
  severity?: DashSeverity | null
}

export interface BucketItem {
  id: string
  label: string
  value: number
  sub?: string | null
  severity?: DashSeverity | null
  href?: string | null
}

export interface BucketSection {
  id: string
  label: string
  items: BucketItem[]
  icon_id?: string | null
  empty_hint?: string | null
}

export interface DashboardSummaryV2 {
  schema_version: 2
  window: "24h" | "7d" | "30d"
  generated_at: string
  kpis: KpiCard[]
  top_buckets: BucketSection[]
}

// ── Provider Platforms ────────────────────────────────────────────

export interface AuthFieldRead {
  key: string
  label: string
  type: "string" | "secret" | "url" | "bool" | "select"
  required: boolean
  help_text?: string | null
  options?: string[] | null
}

export interface StreamRead {
  stream: string
  schedule_seconds: number
}

export interface ProviderPlatformRead {
  platform: string
  display_name: string
  /** Categoria do catálogo (agrupa na galeria): "EDR / XDR", "Identity", etc. */
  category: string
  /** Descrição curta (self-describing pelo plugin do vendor). */
  description: string
  icon_id?: string | null
  docs_url?: string | null
  auth_fields: AuthFieldRead[]
  streams: StreamRead[]
  /** Suporta "Testar conexão" pré-save (o vendor declarou um probe). */
  supports_test?: boolean
  /** "pull" (poll de API, default) | "push" (fonte empurra p/ /api/ingest). */
  transport?: string
}

export interface TestConnectionResponse {
  status: string
  details: Record<string, any>
}

export interface Alert {
  alert_id: string
  title: string
  severity: string
  platform: string
  timestamp?: string
  hostname?: string
  rule_id?: string
  rule_level?: number
  rule_groups: string[]
  rule_firedtimes?: number
  mitre_ids: string[]
  mitre_tactics: string[]
  mitre_techniques: string[]
  decoder_name?: string
  agent_id?: string
  agent_name?: string
  agent_ip?: string
  agent_group?: string
  agent_labels: Record<string, any>
  manager_name?: string
  location?: string
  full_log?: string
  src_ip?: string
  dst_ip?: string
  src_user?: string
  dst_user?: string
  input_type?: string
  syscheck_path?: string
  data_fields: Record<string, any>
  highlights: Record<string, string[]>
  source_index?: string
  integration_id?: number
  integration_name?: string
  organization_id?: number
  organization_name?: string
  raw: Record<string, any>
}

export interface AlertDetail extends Alert {}

export interface AlertListResponse {
  items: Alert[]
  total: number
  limit: number
  offset: number
  has_more: boolean
}

export interface AggregatedAlertListResponse extends AlertListResponse {
  partial_errors: string[]
  is_sampled: boolean
}

export interface AlertFilters {
  index?: string
  severity?: string
  level?: string
  hostname?: string
  agent_id?: string
  rule_id?: string
  rule_group?: string
  decoder?: string
  src_ip?: string
  dst_ip?: string
  username?: string
  description?: string
  description_mode?: "smart" | "exact" | "contains"
  query?: string
  time_from?: string
  time_to?: string
  limit?: number
  offset?: number
}

export interface ProviderOperationError {
  code: string
  message: string
  integration_id?: number | null
  details?: Record<string, any>
}

export interface IntegrationOverviewAlertPreview {
  alert_id: string
  title: string
  severity: string
  timestamp?: string
  hostname?: string
  rule_id?: string
  source_index?: string
}

/**
 * Produto licenciado retornado por GET /integrations/{id}/overview.
 * Presente SOMENTE quando platform=sophos AND kind=tenant AND parent_integration_id IS NOT NULL.
 *
 * Fonte: GET https://api.central.sophos.com/licenses/v1/licenses (Sophos Licensing API v1).
 */
export interface LicensedProduct {
  /** SKU code retornado pela Sophos (ex.: "CIXAXDR", "CIXAMTR-ADV-MSP", "CIXA-MSP"). */
  code: string
  /** Nome oficial do produto retornado pela Sophos (ex.: "Sophos XDR - User"). */
  label: string
  /**
   * Categoria coarse derivada pelo backend.  Útil para badges destacadas e
   * para responder "essa tenant pode coletar detections/cases?":
   *   "xdr"  → tenant pode usar /detections/v1/queries/detections
   *   "mdr"  → tenant pode usar /cases/v1/cases (Sophos MDR)
   *   null   → outro produto (Endpoint, Email, Encryption, hardware…)
   */
  category?: "xdr" | "mdr" | null
  /** Campos adicionais retornados pela API Sophos (quantity, dates, usage, …). */
  details: {
    type?: string | null
    quantity?: number | null
    unlimited?: boolean
    perpetual?: boolean
    startDate?: string | null
    endDate?: string | null
    usageCount?: number | null
    licenseIdentifier?: string | null
  } & Record<string, unknown>
}

export interface IntegrationOverview {
  integration: Integration
  health?: {
    status: string
    details: Record<string, any>
    manager_status?: string | null
    indexer_status?: string | null
  } | null
  alerts_preview?: {
    items: IntegrationOverviewAlertPreview[]
  } | null
  alerts_preview_error?: ProviderOperationError | null
  /**
   * Lista de produtos licenciados do tenant Sophos child.
   * null  → integração não é um child tenant (seção não deve ser renderizada).
   * []    → é um child tenant mas a API não retornou produtos.
   */
  licensed_products?: LicensedProduct[] | null
}

export interface DashboardAlertSeveritySummary {
  critical: number
  high: number
  medium: number
  low: number
  info: number
}

export interface DashboardAlertTrendPoint extends DashboardAlertSeveritySummary {
  timestamp?: string
  total: number
}

export interface DashboardAlertSourceSummary {
  integration_id: number
  integration_name: string
  organization_id: number
  organization_name?: string
  total: number
  by_severity: DashboardAlertSeveritySummary
}

export interface DashboardAlertBucketSummary {
  key: string
  label?: string | null
  count: number
  integration_id?: number | null
  integration_name?: string | null
  organization_id?: number | null
  organization_name?: string | null
}

export interface DashboardIntegrationIssueSummary {
  integration_id: number
  integration_name: string
  organization_id: number
  organization_name?: string | null
  status: string
  last_error?: string | null
  last_checked_at?: string | null
}

export interface DashboardMetricComparison {
  current: number
  previous: number
  delta: number
  trend: "up" | "down" | "stable"
}

export interface DashboardAlertComparisonSummary {
  total_alerts: DashboardMetricComparison
  critical_alerts: DashboardMetricComparison
}

export interface DashboardIntegrationComparisonSummary {
  degraded_integrations: DashboardMetricComparison
}

export interface DashboardPrioritySummary {
  organization_id?: number | null
  organization_name?: string | null
  integration_id?: number | null
  integration_name?: string | null
  critical: number
  high: number
  total: number
}

export interface DashboardSummary {
  organizations: {
    total: number
    active: number
  }
  integrations: {
    total: number
    active: number
    authenticated: number
    by_platform: Record<string, number>
    health: {
      healthy: number
      degraded: number
      error: number
      unknown: number
      inactive?: number
    }
    degraded_items: DashboardIntegrationIssueSummary[]
    comparison: DashboardIntegrationComparisonSummary
  }
  alerts: {
    total: number
    by_severity: DashboardAlertSeveritySummary
    trend: DashboardAlertTrendPoint[]
    sources: DashboardAlertSourceSummary[]
    top_hosts: DashboardAlertBucketSummary[]
    top_rules: DashboardAlertBucketSummary[]
    top_mitre_ids: DashboardAlertBucketSummary[]
    top_agent_groups: DashboardAlertBucketSummary[]
    partial_errors: string[]
    latest_timestamp?: string | null
    last_query_at?: string | null
    unsupported_sources: number
    window_days: number
    applied_organization_id?: number | null
    applied_integration_id?: number | null
    applied_platform?: PlatformType | null
    comparison: DashboardAlertComparisonSummary
    most_critical_client?: DashboardPrioritySummary | null
    most_critical_integration?: DashboardPrioritySummary | null
  }
}

export interface Client {
  id: number
  name: string
  region?: string
  client_id?: string
  tenant_id?: string
  is_authenticated: boolean
}

export interface AuthTokens {
  access_token: string
  refresh_token: string
}

export interface AuthStatus {
  setup_required: boolean
  company_name: string
  company_portal_name: string
  /** SSO Microsoft Entra habilitado (mostra o botão de login federado). */
  sso_enabled?: boolean
  /** Rótulo configurável do botão de SSO. */
  sso_button_label?: string | null
}

export interface AuthUser {
  id: string
  username: string
  email?: string | null
  display_name?: string
  /** Origem da identidade: "local" (senha) ou "entra" (SSO). */
  auth_provider?: string
  /** Escopo global de leitura (vê todas as orgs sem ser admin). */
  is_global?: boolean
  organization_id?: number | null
  organization_name?: string | null
  role: UserRole
  is_active: boolean
  /** Preferência de idioma da UI ("pt"/"en"/"es") ou null (seguir o navegador). */
  locale?: string | null
  permissions: string[]
}

export interface AppUser extends AuthUser {
  created_at: string
  updated_at: string
  last_login_at?: string | null
}

// ── Self-service account (própria conta do usuário logado) ────────────

/** Perfil próprio devolvido por GET /auth/me/profile — superset seguro do
 *  usuário de sessão com carimbos úteis ao dono. Nunca traz segredos. */
export interface AccountProfile extends AuthUser {
  created_at: string
  last_login_at?: string | null
}

/** Corpo de PATCH /auth/me — só os campos que o dono pode alterar em si mesmo.
 *  `current_password` é exigido apenas ao trocar o e-mail (reautenticação). */
export interface SelfProfileUpdate {
  display_name?: string | null
  email?: string | null
  locale?: string | null
  current_password?: string
}

/** Corpo de POST /auth/me/password. */
export interface PasswordChangeRequest {
  current_password: string
  new_password: string
}

export interface PasswordChangeResult {
  detail: string
  revoked_other_sessions: number
}

export interface RevokeOtherSessionsResult {
  revoked: number
}

export interface LoginRequest {
  username: string
  password: string
}

export interface BootstrapAdminRequest extends LoginRequest {
  display_name?: string
}

export interface LoginResponse {
  user: AuthUser
  expires_at: string
}

// ── Identity / SSO config (operada pela UI) ──────────────────

export interface IdentityConfig {
  entra_enabled: boolean
  entra_tenant_id?: string | null
  entra_client_id?: string | null
  /** O valor do secret nunca é devolvido — só esta flag. */
  entra_client_secret_configured: boolean
  entra_redirect_uri?: string | null
  entra_authority: string
  entra_scopes: string
  entra_role_map: Record<string, string>
  entra_default_role: string
  entra_default_is_global: boolean
  entra_jit_provisioning: boolean
  entra_allowed_email_domains: string[]
  entra_button_label: string
  entra_post_login_redirect: string
  // campos de sincronização Graph
  entra_sync_enabled: boolean
  entra_sync_deprovision: boolean
  entra_last_sync_at?: string | null
  entra_last_sync_status?: 'ok' | 'error' | 'running' | 'never' | null
  entra_last_sync_summary?: EntraSyncSummary | null
  is_persisted: boolean
  updated_at?: string | null
}

export interface UpdateIdentityConfigRequest {
  entra_enabled?: boolean
  entra_tenant_id?: string | null
  entra_client_id?: string | null
  /** Envie apenas para trocar o secret; omita para preservar o atual. */
  entra_client_secret?: string
  entra_redirect_uri?: string | null
  entra_authority?: string
  entra_scopes?: string
  entra_role_map?: Record<string, string>
  entra_default_role?: string
  entra_default_is_global?: boolean
  entra_jit_provisioning?: boolean
  entra_allowed_email_domains?: string[]
  entra_button_label?: string
  entra_post_login_redirect?: string
  // toggles de sync
  entra_sync_enabled?: boolean
  entra_sync_deprovision?: boolean
}

export interface IdentityConnectionTestResult {
  ok: boolean
  detail: string
}

// ── status de sync Graph ─────────────────────────────────────

export interface EntraSyncSummary {
  created: number
  updated: number
  deactivated: number
  errors: string[]
  started_at?: string | null
  finished_at?: string | null
}

export interface EntraSyncStatus {
  last_sync_at?: string | null
  last_sync_status?: 'ok' | 'error' | 'running' | 'never' | null
  last_sync_summary?: EntraSyncSummary | null
  lock_active: boolean
}

export interface EntraSyncTriggerResult {
  queued: boolean
  message: string
  lock_active: boolean
}

export interface CreateUserRequest {
  username: string
  password: string
  display_name?: string
  organization_id?: number | null
  role: UserRole
}

export interface UpdateUserRequest {
  username?: string
  password?: string
  display_name?: string
  organization_id?: number | null
  role?: UserRole
  is_active?: boolean
}

export interface SearchFormData {
  clients: number[]
  statement: string
  from: string
  to: string
}

export interface SearchQuery {
  statement: string
  from_: string
  to: string
}

export type QueryRequest = SearchQuery

export interface SearchJob {
  clientId: number
  clientName: string
  searchId?: string
  status: string
  resultCount: number
  error?: string
  results: any[]
}

export interface SearchExecutionClient {
  client_id: number
  client_name: string
  search_id?: string
  status: string
  result_count: number
  error?: string
}

export interface SearchExecutionSummary {
  total_clients: number
  successful_clients: number
  failed_clients: number
  clients_with_results: number
  total_results: number
}


export interface HistoryItem {
  id: number
  client_id?: number
  operation: string
  endpoint: string
  timestamp: string
  payload?: string
  response_summary?: string
}

export interface AuditHistoryItem {
  id: number
  user_id?: number
  username?: string
  user_role?: UserRole
  action: string
  endpoint: string
  method?: string
  status_code?: number
  ip_address?: string
  user_agent?: string
  request_payload?: string
  detail?: string
  created_at: string
}

export interface AuditFilters {
  username?: string
  ip_address?: string
  date_from?: string
  date_to?: string
}

export interface SearchHistoryItem {
  id: number
  search_id: string
  client_id?: number
  schedule_id?: number
  status: string
  result_json?: string
  statement: string
  table: string
  from_ts: string
  to_ts: string
  engine?: string
  language?: string
  error_message?: string
  result_count?: number | null
  created_at: string
}

export interface Query {
  id: number
  title: string
  description?: string
  statement: string
  table: string
  client_ids?: number[]
  /** dialeto da query salva (↔ QueryCapability.dialect). Opcional. */
  dialect?: QueryDialect
  /** passthrough (default) ou sigma. Opcional. */
  spec_kind?: QuerySpecKind
}

export interface CreateQueryRequest {
  title: string
  description?: string
  statement: string
  table: string
  client_ids?: number[]
  dialect?: QueryDialect
  spec_kind?: QuerySpecKind
}

export interface UpdateQueryRequest {
  title?: string
  description?: string
  statement?: string
  table?: string
  client_ids?: number[]
  dialect?: QueryDialect
  spec_kind?: QuerySpecKind
}

export interface Schedule {
  id: number
  query_id: number
  query_title?: string
  client_ids: number[]
  interval_value: number
  interval_unit: ScheduleTimeUnit
  lookback_value?: number
  lookback_unit?: ScheduleTimeUnit
  notify_on_results?: boolean
  days_back?: number
  next_run: string
  last_run_at?: string | null
  created_at?: string | null
  updated_at?: string | null
}

export interface CreateScheduleRequest {
  query_id: number
  client_ids: number[]
  interval_value: number
  interval_unit: ScheduleTimeUnit
  lookback_value: number
  lookback_unit: ScheduleTimeUnit
  notify_on_results: boolean
  days_back?: number
}

export interface EmailRecipient {
  id: number
  email: string
}

export interface CreateEmailRequest {
  email: string
}

export interface EmailConfig {
  id?: number
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_password_configured?: boolean
  use_tls: boolean
  sender: string
}

export interface UpdateEmailConfigRequest {
  smtp_host?: string
  smtp_port?: number
  smtp_user: string
  smtp_password?: string
  clear_smtp_password?: boolean
  use_tls?: boolean
  sender?: string
}

export interface TableColumn<T = any> {
  key: string
  title: React.ReactNode
  dataIndex: string
  render?: (value: any, record: T, index: number) => React.ReactNode
  width?: number | string
  sortable?: boolean
  align?: "left" | "center" | "right"
  /** Tailwind classes aplicadas ao <th> e <td> desta coluna (ex: "hidden sm:table-cell"). */
  className?: string
}

export interface PaginationConfig {
  current: number
  pageSize: number
  total?: number
  showSizeChanger?: boolean
  showQuickJumper?: boolean
  showTotal?: boolean
}

export interface SelectOption {
  value: string | number
  label: string
  disabled?: boolean
  description?: string
}

export interface NavigationItem {
  key: string
  label: string
  path: string
  icon: React.ReactNode
}

export type FormErrors<T> = {
  [K in keyof T]?: string
}

export interface UseFormConfig<T> {
  initialValues: T
  validate?: (values: T) => FormErrors<T>
  onSubmit: (values: T) => Promise<void>
}

export interface ApiError {
  detail: string
  status_code?: number
}

// ── Collector Multi-Tenant ──────────────────────────────────────────
// Espelha os schemas Pydantic em backend/app/api/schemas.py
// (CollectorVendorRead, CollectionStateRead, CollectorSummary, …).

export interface CollectorVendor {
  platform: string
  stream: string
  queue: string
  task_name: string
  schedule_seconds: number
}

export interface CollectionState {
  integration_id: number
  integration_name?: string | null
  organization_id?: number | null
  organization_name?: string | null
  platform?: string | null
  stream: string
  cursor?: Record<string, unknown> | null
  last_success_at?: string | null
  last_attempt_at?: string | null
  last_error?: string | null
  consecutive_failures: number
  events_collected_total: number
  updated_at?: string | null
}

export interface CollectorPerPlatformSummary {
  platform: string
  integrations: number
  events_collected_total: number
  errors: number
}

export interface CollectorSummary {
  integrations_tracked: number
  vendors_registered: number
  events_collected_total: number
  integrations_with_errors: number
  stale_minutes_max?: number | null
  per_platform: CollectorPerPlatformSummary[]
}

export interface CollectorTriggerResponse {
  task_id: string
  queue: string
  integration_id: number
  stream: string
}

// ── Collector Config (gerenciado na UI /config) ─────────────────────
// Espelha backend/app/api/schemas.py::CollectorConfig*.

export type DispatchMode = "syslog" | "jsonl" | "both"

export type WazuhSyslogFormat = "rfc3164" | "rfc5424"

export interface CollectorRateLimits {
  per_second?: number
  per_minute?: number
  per_hour?: number
  per_day?: number
}

export interface CollectorConfig {
  id: number
  is_persisted: boolean
  config_version: string
  updated_at?: string | null

  wazuh_syslog_host?: string | null
  wazuh_syslog_port: number
  wazuh_syslog_use_tls: boolean
  wazuh_ca_bundle?: string | null
  wazuh_dispatch_mode: DispatchMode
  wazuh_syslog_format: WazuhSyslogFormat
  collector_jsonl_dir: string

  collector_batch_size: number
  collector_batch_flush_seconds: number
  dedupe_ttl_days: number

  domain_concurrency_limits: Record<string, number>
  rate_limits_by_vendor: Record<string, CollectorRateLimits>
}

export interface UpdateCollectorConfigRequest {
  wazuh_syslog_host?: string | null
  wazuh_syslog_port?: number
  wazuh_syslog_use_tls?: boolean
  wazuh_ca_bundle?: string | null
  wazuh_dispatch_mode?: DispatchMode
  wazuh_syslog_format?: WazuhSyslogFormat
  collector_jsonl_dir?: string
  collector_batch_size?: number
  collector_batch_flush_seconds?: number
  dedupe_ttl_days?: number
  domain_concurrency_limits?: Record<string, number>
  rate_limits_by_vendor?: Record<string, CollectorRateLimits>
}

export interface CollectorConfigTestResult {
  component: "syslog" | "jsonl"
  status: "healthy" | "error" | "skipped"
  details: Record<string, unknown>
}

export interface CollectorConfigTestResponse {
  mode: DispatchMode
  results: CollectorConfigTestResult[]
}

// ── Collector Audit (ring buffer dos últimos eventos despachados) ───
// Espelha backend/app/api/schemas.py::CollectorAudit*.

export interface CollectorAuditEventMeta {
  integration_id?: number | string | null
  customer_id?: number | string | null
  /** OCSF v1.0 usa `vendor`; envelope legado usava `platform`. Ambos são suportados. */
  vendor?: string | null
  platform?: string | null
  /** Granular (ex: `sophos.alert`). Legado usa `stream`. */
  event_type?: string | null
  stream?: string | null
  collected_at?: string | null
}

export interface CollectorAuditEnvelope {
  hostname?: string | null
  pri?: number | null
}

export interface CollectorAuditEvent {
  event: Record<string, unknown>
  envelope: CollectorAuditEnvelope
  meta: CollectorAuditEventMeta
  /** Formato syslog usado no dispatch deste evento.
   *  null = evento legado, gravado antes deste campo (tratar como rfc5424). */
  syslog_format?: "rfc3164" | "rfc5424" | null
}

export interface CollectorAuditResponse {
  count: number
  events: CollectorAuditEvent[]
}

// ── Edição / licença (open-core) ─────────────────────────────
// Espelha o backend GET /api/edition. NÃO expõe o customer id (sub) por design.
export interface EditionStatus {
  edition: string // "community" | "enterprise"
  features: string[]
  plan?: string | null
  seats?: number | null
  /** Teto de orgs do tier (null = ilimitado; Starter single-tenant = 1). */
  max_organizations?: number | null
  /** ISO-8601 ou null. */
  expires_at?: string | null
  /** True = licença venceu mas está na JANELA DE CARÊNCIA (renovar!); depois → Community. */
  expired_in_grace?: boolean
}

/** Edição + metadados de ativação da licença (/api/licenses/status). */
export interface LicenseStatus extends EditionStatus {
  /** Origem do token ativo: 'database' (ativado pela UI), 'environment' (env/arquivo) ou 'none'. */
  source: "database" | "environment" | "none"
  activated_by?: string | null
  activated_at?: string | null
}

// ── Captura ao vivo ("listening mode") ────────────────────────────────
// Sessão efêmera (Redis ring) que grava uma amostra do tráfego despachado,
// opcionalmente escopada a um vendor. Para inspecionar o shape real sem
// vasculhar o ring global de auditoria.
export interface CaptureSession {
  id: string
  organization_id?: number | null
  vendor?: string | null
  /** epoch seconds */
  created_at?: number | null
  /** epoch seconds */
  expires_at?: number | null
  status: "active" | "stopped" | "expired" | string
  event_count: number
}

export interface CaptureSessionList {
  count: number
  sessions: CaptureSession[]
}

export interface CaptureEvent {
  event: Record<string, unknown>
  vendor?: string | null
  /** epoch seconds */
  captured_at?: number | null
}

export interface CaptureEventList {
  count: number
  session_id: string
  events: CaptureEvent[]
}

export interface CaptureSessionStartRequest {
  vendor?: string
  duration_seconds?: number
  ring_size?: number
}

export type Optional<T, K extends keyof T> = Omit<T, K> & Partial<Pick<T, K>>
export type RequiredFields<T, K extends keyof T> = T & Required<Pick<T, K>>

// ── DSL v2 Preprocess ────────────────────────────────────────────

/**
 * PreprocessOp — operação executada ANTES das regras para popular campos
 * virtuais (prefixo `_`) com dados pré-processados.
 * Hoje o único `op` disponível é `json_parse`; a union cresce com novos ops.
 */
export interface PreprocessOp {
  op: "json_parse"
  source: string
  target: string     // deve começar com _
  tolerant: boolean
}

/**
 * MappingPayload — shape único aceito pelo backend (DSL v2).
 * `preprocess` é sempre uma lista (vazia se não houver pré-processamento).
 */
export interface MappingPayload {
  preprocess: PreprocessOp[]
  rules: MappingRule[]
}

// ── Normalização / Mapping ──────────────────────────────────────────

export type Permission = string // backend valida; frontend trata como opaque

/**
 * TypeCastDescriptor — descreve uma função de cast registrada no backend.
 * Retornado por GET /api/mappings/normalize/type-casts (array, ordenado por nome).
 */
export interface TypeCastDescriptor {
  name: string
  description: string
  signature: string
}

export interface Mapping {
  id: string
  vendor: string
  event_type: string
  description?: string | null
  current_version_id: string | null
  created_at: string
  updated_at: string
}

/**
 * KnownTypeCast — alias documental dos casts conhecidos.
 * O conjunto real é dinâmico (registro no backend); o campo type_cast em
 * MappingRule é `string | null` para acomodar qualquer nome que o backend
 * expuser via GET /api/mappings/normalize/type-casts.
 */
export type KnownTypeCast =
  | "dedup"
  | "epoch_to_iso"
  | "iso_to_epoch"
  | "lowercase"
  | "mitre_tactic_to_ocsf"
  | "score_to_percent"
  | "to_array"
  | "to_bool"
  | "to_int"
  | "to_str"
  | "trim"
  | "uppercase"

/**
 * MappingPredicate — AST de predicado booleano para o campo ``when``.
 *
 * Um predicate tem EXATAMENTE UMA chave discriminadora:
 * - ``exists``: JMESPath; verdadeiro se o valor não for null.
 * - ``equals``: compara o valor de ``source`` com ``value`` (type-strict: 3 !== "3").
 * - ``in``: verifica se o valor de ``source`` está em ``values`` (type-strict).
 * - ``not``: negação; permite nesting.
 *
 * DSL v2 apenas.  Quando ``when`` avalia para false, a regra é IGNORADA —
 * o target não é escrito (semântica diferente de ``default`` ou null).
 */
export type MappingPredicate =
  | { exists: string }
  | { equals: { source: string; value: unknown } }
  | { in: { source: string; values: unknown[] } }
  | { not: MappingPredicate }

/**
 * ScalarMappingRule — regra de mapeamento 1-para-1 (comportamento padrão).
 * ``kind`` é omitido ou ``"scalar"`` — NUNCA serializado como ``"scalar"``
 * pelo backend (ausência = scalar por padrão).
 */
export interface ScalarMappingRule {
  target: string
  /** Omitido em scalar rules — ausência implica kind="scalar". */
  kind?: "scalar"
  source?: string | null // JMESPath
  const?: unknown
  default?: unknown
  /** Cast aplicado ANTES de value_map — mesmo conjunto de KnownTypeCast. */
  pre_cast?: string | null
  value_map?: Record<string, unknown>
  /** Dynamic set — widened to string | null. See KnownTypeCast for documentation. */
  type_cast?: string | null
  required?: boolean
  /**
   * fallback_source — lista ordenada de JMESPath tentados quando
   * o ``source`` primário resolve null.  DSL v2 apenas.
   * Exemplo: ["detectionRule", "attackType"]
   */
  fallback_source?: string[]
  /**
   * when — predicado que guarda a regra.  DSL v2 apenas.
   * Se avaliado como false, a regra é IGNORADA (target não é escrito).
   * Diferente de ``default``: o target fica completamente ausente do output.
   */
  when?: MappingPredicate | null
  /**
   * expected_always_default — flag diagnóstico.
   * Se true, suprime o warning de "100% default hit" no dry-run para esta
   * regra. Usar quando o source intencionalmente nunca existe (placeholder).
   * Não altera o comportamento em runtime.
   */
  expected_always_default?: boolean
}

/**
 * ArrayBuilderItem — uma entrada no array de observables.
 *
 * Campos de um item:
 * - ``name``: rótulo do campo observable (ex: "src_ip").
 * - ``type``: string label OCSF (ex: "IP Address").
 * - ``type_id``: código inteiro OCSF (ex: 2 para IP Address).
 * - ``source``: JMESPath; pode referenciar raw ou ``_extracted`` (prefixo ``_``).
 * - ``explode``: se true e source resolver lista, gera 1 observable por elemento.
 * - ``skip_null``: se true, itens com source null são omitidos.
 *   Herda do nível da regra quando ausente.
 */
export interface ArrayBuilderItem {
  name: string
  type: string
  type_id: number
  source: string
  explode?: boolean
  skip_null?: boolean
}

/**
 * ArrayBuilderRule — regra que constrói um array de
 * observables OCSF a partir de múltiplos campos do payload.
 *
 * DSL v2 apenas.  Campos de scalar rule (``source``, ``const``, ``value_map``,
 * ``default``, ``type_cast``, ``pre_cast``, ``fallback_source``, ``when``)
 * são PROIBIDOS neste nível — configure via ``items``.
 *
 * Caso de uso canônico: popular ``normalized.observables`` com IPs, emails
 * e hashes extraídos do Sophos Detection Event.
 */
export interface ArrayBuilderRule {
  target: string
  kind: "array_builder"
  items: ArrayBuilderItem[]
  /**
   * skip_null (default true) — herdado pelos items sem skip_null próprio.
   * Se true, items com source null não geram observable.
   * Se false, observable com ``value: null`` é produzido (raro).
   */
  skip_null?: boolean
  /**
   * dedup_by — nomes de campos dentro de cada observable para deduplicação
   * (first-wins).  Exemplo: ["value"].  Não usa JMESPath — apenas nomes
   * diretos de campos do observable dict.
   */
  dedup_by?: string[]
}

/**
 * MappingRule — union de todos os kinds de regras suportados.
 *
 * Use narrowing para acessar campos específicos:
 *   if (rule.kind === "array_builder") { rule.items ... }
 *   else { rule.source ... }
 *
 * Consumidores existentes que lêem ``rule.source`` devem verificar
 * ``rule.kind !== "array_builder"`` antes de acessar campos de scalar.
 *
 * Alias de compatibilidade: ``MappingRule`` era anteriormente a interface
 * scalar; agora é a union.  Código que precisava de scalar explicitamente
 * pode usar ``ScalarMappingRule``.
 */
export type MappingRule = ScalarMappingRule | ArrayBuilderRule

export interface MappingVersion {
  id: string
  definition_id: string
  version_number: number
  /** Shape v2: { preprocess, rules }. Sempre dict. */
  rules: MappingPayload
  author_user_id: number | null
  commit_message: string
  diff_from_previous: unknown
  dry_run_stats: unknown
  created_at: string
}

export interface DryRunRuleStats {
  target: string
  fail_count: number
  fail_examples: string[]
}

/**
 * DryRunDefaultHitWarning — regra cujo source resolve None
 * em 100% das amostras e o engine SEMPRE cai no ``default``.
 *
 * Indica JMESPath potencialmente errado, vendor que mudou o shape, ou campo
 * genuinamente ausente nas amostras. Regras com ``expected_always_default: true``
 * NÃO aparecem aqui.
 */
export interface DryRunDefaultHitWarning {
  target: string
  hit_rate: number
  hit_count: number
  sample_size: number
  expected_always_default: boolean
}

export interface DryRunResult {
  sample_size: number
  ok_count: number
  fail_count: number
  rule_failures: DryRunRuleStats[]
  output_examples: Record<string, unknown>[]
  /** regras com 100% default hit rate (sem expected_always_default). */
  default_hit_warnings: DryRunDefaultHitWarning[]
}

export interface DriftEntry {
  id: string
  vendor: string
  event_type: string
  field_path: string
  sample_value: string | null
  sample_type: string | null
  occurrence_count: number
  first_seen: string
  last_seen: string
  status: "new" | "ignored" | "mapped"
}

export interface QuarantineEntry {
  id: string
  integration_id: number | null
  vendor: string
  event_type: string | null
  error_kind: string
  error_detail: string | null
  mapping_version_id: string | null
  created_at: string
  expires_at: string
  reprocessed_at: string | null
}

export interface QuarantineDetail extends QuarantineEntry {
  raw_payload: unknown
}

export interface MappingAuditEntry {
  id: string
  mapping_definition_id: string | null
  mapping_version_id: string | null
  integration_id?: number | null
  action: string
  user_id: number | null
  username: string | null
  user_role: string | null
  diff: unknown
  detail: string | null
  created_at: string
}

// ── Sprint 2: Backfill ───────────────────────────────────────────────

export type BackfillJobStatus = "pending" | "running" | "completed" | "failed" | "cancelled"

export interface BackfillJob {
  id: string
  integration_id: number
  streams: string[]
  from_ts: string
  to_ts: string
  status: BackfillJobStatus
  events_collected: number
  events_dispatched: number
  progress_pct: number
  requested_by_user_id: number | null
  requested_at: string
  started_at: string | null
  finished_at: string | null
  last_error: string | null
  cancelled_at: string | null
}

export interface CreateBackfillJobRequest {
  streams: string[]
  from_ts: string
  to_ts: string
}

// ── Sprint 5: Pipeline Health ────────────────────────────────────────

export type PipelineHealthStatus = "healthy" | "degraded" | "unhealthy" | "unknown"

export interface IntegrationPipelineHealth {
  integration_id: number
  status: PipelineHealthStatus
  events_per_minute: number | null
  lag_seconds: number | null
  last_error: string | null
  last_success_at: string | null
  mapped_field_ratio: number | null
  drift_count_24h: number
  quarantine_count_24h: number
  cached_at: string
}

// ── Personal Access Tokens (PAT) ──────────────────────────────────

// Subset do enum Permission do backend usado como scope identifier.
// Mantido em sync com backend/app/core/auth.py:Permission.
// Não é exhaustive — a UI faz fetch de /api/v1/tokens/scopes pra listar
// dinamicamente; este tipo serve só pra autocomplete/IDE.
export type ScopeName =
  | "mapping.read"
  | "mapping.write"
  | "mapping.rollback"
  | "integration.read"
  | "integration.write"
  | "integration.pause"
  | "quarantine.read"
  | "quarantine.discard"
  | "drift.read"
  | "drift.ignore"
  | "drift.mark_mapped"
  | "drift.delete"
  | "user.manage"
  | "secret.read"
  | "audit.read"
  | "org.manage"
  | "internal.tenant.read"
  | "query.run"
  | "query.save"

export interface ApiToken {
  id: number
  name: string
  token_prefix: string
  // owner discriminator. Apenas um dos dois é populado.
  user_id: number | null
  service_account_id: number | null
  expires_at: string | null
  is_eternal: boolean
  scopes: ScopeName[] | null
  last_used_at: string | null
  last_used_ip: string | null
  use_count: number
  revoked_at: string | null
  created_at: string
}

export interface ApiTokenCreateRequest {
  name: string
  /** ISO datetime; null + is_eternal=true pra "nunca expira". */
  expires_at: string | null
  is_eternal?: boolean
  /** ``null``/omitted = full inherit. Lista vazia = mesmo
   *  efeito (backend normaliza). Ver docs/api-tokens.md pra lista. */
  scopes?: ScopeName[] | null
  /** Define o owner. Omitido = PAT pessoal do usuário logado. */
  service_account_id?: number | null
}

export interface ApiTokenCreateResponse {
  token: string
  api_token: ApiToken
}

// ── Service Accounts ────────────────────────────────────────

export interface ServiceAccount {
  id: number
  name: string
  description: string | null
  role: "viewer" | "operator" | "engineer" | "admin"
  organization_id: number | null
  is_active: boolean
  created_by_user_id: number | null
  created_at: string
  updated_at: string
  active_token_count: number
}

export interface ServiceAccountCreateRequest {
  name: string
  description?: string | null
  role?: "viewer" | "operator" | "engineer" | "admin"
  organization_id?: number | null
}

export interface ServiceAccountUpdateRequest {
  description?: string | null
  role?: "viewer" | "operator" | "engineer" | "admin"
  organization_id?: number | null
  is_active?: boolean
}

// ── Destinos (saída multi-destino) ───────────────────────────

/** Linha da tabela `destinations` (espelha backend DestinationRead). */
export interface Destination {
  id: string
  name: string
  kind: string
  enabled: boolean
  config: Record<string, unknown>
  delivery: Record<string, unknown>
  config_version: string
  organization_id: number | null
  created_at: string
  updated_at: string
  has_secret: boolean
}

export interface DestinationCreateRequest {
  name: string
  kind: string
  config?: Record<string, unknown>
  delivery?: Record<string, unknown>
  enabled?: boolean
  /** WRITE-ONLY — token em claro; cifrado no backend, nunca retornado. */
  hec_token?: string | null
  organization_id?: number | null
}

export interface DestinationUpdateRequest {
  name?: string
  config?: Record<string, unknown>
  delivery?: Record<string, unknown>
  enabled?: boolean
  hec_token?: string | null
  organization_id?: number | null
}

/** Um JSON Schema (Pydantic model_json_schema) — forma genérica. */
export interface JsonSchema {
  type?: string
  properties?: Record<string, JsonSchemaProperty>
  required?: string[]
  $defs?: Record<string, JsonSchema>
  [key: string]: unknown
}

export interface JsonSchemaProperty {
  type?: string
  title?: string
  description?: string
  default?: unknown
  enum?: unknown[]
  minimum?: number
  maximum?: number
  anyOf?: JsonSchemaProperty[]
  allOf?: JsonSchemaProperty[]
  $ref?: string
  [key: string]: unknown
}

/** Entrada do catálogo `GET /collectors/destinations/destination-types`. */
export interface DestinationType {
  kind: string
  label: string
  default_queue: string
  capabilities: string[]
  required_secrets: string[]
  config_schema: JsonSchema
  delivery_schema: JsonSchema
  delivery_defaults: Record<string, unknown>
  /** Catálogo self-describing (simetria com ProviderPlatformRead) — a galeria lê
   *  ícone/categoria/descrição/tier DAQUI, sem mapas hardcoded. */
  category?: string
  description?: string
  icon_id?: string | null
  docs_url?: string | null
  /** "stable" | "beta" | "generic" */
  tier?: string
  order?: number
}

export interface DestinationTestResult {
  ok: boolean
  detail: string
  latency_ms: number | null
}

export interface DestinationShadowResult {
  ok: boolean
  detail: string
  count: number
  formatted_preview: string | null
  latency_ms: number | null
}

export type DestinationHealthStatus =
  | "healthy"
  | "degraded"
  | "unhealthy"
  | "disabled"
  | "unknown"

export interface DestinationHealth {
  destination_id: string
  status: DestinationHealthStatus
  enabled: boolean
  breaker_state: string | null
  dlq_total: number
  dlq_24h: number
  last_dlq_at: string | null
  eps: number | null
  bytes_per_min: number | null
}

// ── saúde em lote + topologia com throughput ─

/** Estado do circuit breaker por destino. */
export type DestinationBreakerState = "closed" | "open" | "half_open" | "unknown"

/** Item de `GET /collectors/destinations/health` (saúde em lote, 1 chamada). */
export interface DestinationHealthItem {
  destination_id: string
  name: string
  kind: string
  status: DestinationHealthStatus
  enabled: boolean
  breaker_state?: DestinationBreakerState | null
  dlq_total: number
  dlq_24h: number
  last_dlq_at?: string | null
  eps?: number | null
  bytes_per_min?: number | null
}

/** Resposta de `GET /collectors/destinations/health`. */
export interface DestinationHealthBatchResponse {
  total: number
  items: DestinationHealthItem[]
}

/** Nó de destino na topologia de roteamento. */
export interface TopologyDestination {
  id: string
  name: string
  kind: string
  status: DestinationHealthStatus
  eps?: number | null
  bytes_per_min?: number | null
}

/** Aresta lógica (rota) na topologia de roteamento. */
export interface TopologyRoute {
  id: string
  name: string
  action: string
  destination_ids: string[]
  matched_per_min: number
  routed_per_min: number
  drop_per_min: number
  enabled: boolean
  is_system: boolean
}

/** Resposta de `GET /collectors/routes/topology`. */
export interface RoutingTopologyResponse {
  destinations: TopologyDestination[]
  routes: TopologyRoute[]
}

// ── Flow-view (página /flow — grafo sources→rotas→destinos com volume) ────────
export type FlowNodeStatus = "healthy" | "degraded" | "unhealthy" | "unknown"

export interface FlowSource {
  id: string
  name: string
  platform: string
  status: FlowNodeStatus
  events_per_minute: number
  eps: number
}

export interface FlowTotals {
  ingest_eps: number
  routed_per_min: number
  drop_per_min: number
  delivered_eps: number
}

export interface FlowGraph {
  generated_at: string
  window_minutes: number
  sources: FlowSource[]
  routes: TopologyRoute[]
  destinations: TopologyDestination[]
  totals: FlowTotals
}


// ── observabilidade por destino ───────────────────────

export interface DestinationDlqEntry {
  id: string
  event_id: string
  error_kind: string
  error_detail: string | null
  payload: Record<string, unknown> | null
  organization_id: number | null
  created_at: string
}

export interface DestinationDlqResponse {
  destination_id: string
  total: number
  by_error_kind: Record<string, number>
  entries: DestinationDlqEntry[]
}

export interface DestinationTap {
  destination_id: string
  entries: Record<string, unknown>[]
}

export interface DestinationMetrics {
  destination_id: string
  available: boolean
  reason: string | null
  /** série lógica (sent/rejected/latency_avg) → [[minute_epoch, valor], ...]. */
  series: Record<string, [number, number][]>
  /** gauges instantâneos (queue_depth, backpressure_state). */
  gauges: Record<string, string | null>
  dlq_total: number
  dlq_24h: number
  by_error_kind: Record<string, number>
  breaker_state: string | null
}

// ── Rotas (motor de roteamento) ───────────────────────

export type RouteAction = "route" | "drop"

/** Condição label-driven: {campo: valor} (eq) ou {campo: {op: valor}}. */
export type RouteCondition = Record<string, unknown>

/** ação de redação de PII por campo. */
export type PiiRedactionAction = 'mask' | 'hash' | 'partial' | 'drop_field'

/** Uma regra de redação: mascara/pseudonimiza/remove um campo em raw ou normalized. */
export interface PiiRedactionRule {
  path: string
  action: PiiRedactionAction
  /** mask: largura fixa (não vaza o tamanho). */
  fixed_len?: number
  mask_char?: string
  /** hash: salt opcional (sem salt = reversível por dicionário). */
  salt?: string
  /** partial: mantém prefixo/sufixo (strings) OU N octetos iniciais (IP). */
  keep_prefix?: number
  keep_suffix?: number
  octets?: number
}

/** Spec de redação por rota: objeto versionado ou lista pura (= v1). */
export interface PiiRedactionSpec {
  version?: number
  rules: PiiRedactionRule[]
}

export type PiiRedaction = PiiRedactionSpec | PiiRedactionRule[] | null

export interface Route {
  id: string
  name: string
  priority: number
  condition: RouteCondition
  action: RouteAction
  destination_ids: string[]
  is_final: boolean
  /** Canary rollout 0-100 (100 = full). <100 = só essa fração casa. */
  canary_percent: number
  transform_ref: string | null
  /** redação de PII por rota (null = sem redação). */
  pii_redaction: PiiRedaction
  enabled: boolean
  organization_id: number | null
  created_at: string
  updated_at: string
  /** UX guard — sombreada por uma rota is_final anterior. */
  unreachable: boolean
}

export interface RouteCreateRequest {
  name: string
  condition?: RouteCondition
  destination_ids?: string[]
  action?: RouteAction
  is_final?: boolean
  priority?: number
  enabled?: boolean
  canary_percent?: number
  transform_ref?: string | null
  pii_redaction?: PiiRedaction
  organization_id?: number | null
}

export type RouteUpdateRequest = Partial<RouteCreateRequest>

export interface RouteAudit {
  id: string
  route_id: string
  action: string
  actor: string | null
  snapshot: Record<string, unknown>
  created_at: string
}

export interface RouteDryRunRequest {
  routes?: RouteCreateRequest[] | null
  samples?: Record<string, unknown>[] | null
  sample_size?: number
}

export interface RouteDryRunResult {
  labels: Record<string, unknown>
  destinations: string[]
  dropped: boolean
  fallback: boolean
}

export interface RouteMetrics {
  route_id: string
  /** matched/route/drop → [[minute_epoch, valor], ...] (rollups nativos). */
  series: Record<string, [number, number][]>
}

export interface RouteDryRunResponse {
  evaluated: number
  sample_source: string
  routed: number
  dropped: number
  fallback: number
  per_destination: Record<string, number>
  unreachable_route_ids: string[]
  results: RouteDryRunResult[]
}

// ── novos tipos ────────────────────────────

/** Resposta de POST /collectors/routes/reorder. */
export interface RouteReorderResponse {
  /** Lista de rotas na ordem solicitada, com as novas prioridades. */
  reordered: Route[]
}

/** Resposta de POST /collectors/destinations/{id}/dlq/reprocess. */
export interface DlqReprocessResponse {
  destination_id: string
  /** ID da task Celery enfileirada (vazio quando queued=0). */
  task_id: string
  /** Número de entradas DLQ que serão tentadas. */
  queued: number
}

/** Payload de POST /collectors/destinations/{id}/credential/rotate. */
export interface CredentialRotateRequest {
  /** Novo segredo em texto claro — WRITE-ONLY, nunca retornado. */
  new_secret: string
  /** Timestamp RFC 3339 opcional de expiração da credencial. */
  expires_at?: string | null
}

/** Resposta de POST /collectors/destinations/{id}/credential/rotate. */
export interface CredentialRotateResponse {
  destination_id: string
  secret_version: number
  secret_rotated_at: string
  secret_expires_at: string | null
  has_secret: boolean
}

/** Resposta de POST /collectors/destinations/{id}/credential/revoke. */
export interface CredentialRevokeResponse {
  destination_id: string
  /** Sempre false após revogação. */
  enabled: boolean
  secret_revoked_at: string
  /** Sempre false após revogação. */
  has_secret: boolean
}

/** Uma entrada do log de acesso à credencial. */
export interface CredentialAccessEntry {
  id: string
  destination_id: string
  actor: string | null
  /** decrypt | test | rotate | revoke */
  action: string
  organization_id: number | null
  detail: string | null
  created_at: string
}

/** Resposta paginada de GET /collectors/destinations/{id}/credential/audit. */
export interface CredentialAuditResponse {
  destination_id: string
  total: number
  entries: CredentialAccessEntry[]
}

/** Uma entrega registrada no log de lineage (Redis, TTL 7d por padrão). */
export interface LineageEntry {
  destination_id: string
  kind: string
  /** "delivered" */
  status: string
  /** Epoch UNIX em segundos. */
  ts: number
}

/** Resposta de GET /collectors/destinations/{id}/lineage?event_id=... */
export interface DestinationLineageResponse {
  destination_id: string
  event_id: string
  entries: LineageEntry[]
  retention_note: string
}

/** Resposta de GET /collectors/lineage/{event_id} (admin, org-scoped). */
export interface EventLineageResponse {
  event_id: string
  organization_id: number
  entries: LineageEntry[]
  retention_note: string
}

/** Snapshot de destinos + rotas exportado/importado via config-as-code (GitOps). */
export interface ConfigBundle {
  /** Versão do formato do bundle (ex.: "1.0"). */
  version: string
  /** Timestamp UTC da exportação. */
  exported_at: string
  organization_id: number | null
  /** Destinos (sem secret_ref — has_secret indica presença de credencial). */
  destinations: Destination[]
  routes: Route[]
}

/** Dif de um destino após import. */
export interface DestinationDiff {
  name: string
  /** "created" | "updated" | "unchanged" */
  status: string
  id: string | null
}

/** Dif de uma rota após import. */
export interface RouteDiff {
  name: string
  /** "created" | "updated" | "unchanged" */
  status: string
  id: string | null
}

/** Resposta de POST /collectors/config/import. */
export interface ConfigImportResponse {
  dry_run: boolean
  destinations: DestinationDiff[]
  routes: RouteDiff[]
}

/** Payload de POST /collectors/config/import. */
export interface ConfigImportRequest {
  bundle: ConfigBundle
  /** dest_name → segredo em texto claro; cifrado antes de persistir. */
  secrets?: Record<string, string>
  /** true = validar + diff apenas; false = persistir. */
  dry_run?: boolean
}

// ─────────────────────────────────────────────────────────────────────────────
// Plano de Querys (busca federada, jobs async, detecções, correlação)
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Catálogo de capacidade de query de uma plataforma/dialeto.
 * Fonte: GET /providers/query-capabilities (apenas autenticado).
 */
export interface QueryCapabilityRead {
  dialect: QueryDialect
  /** capability string, ex. "query:opensearch_dsl". */
  capability: string
  /** modos suportados, ex. ["live"] | ["live","data_lake"] | ["passthrough"]. */
  modes: string[]
  supports_async: boolean
  /** janela máxima por query (segundos); null = sem limite declarado. */
  max_window_seconds?: number | null
  /** rate-limit declarado (texto livre, ex. "45/min/tenant"); null = não declarado. */
  rate_limit?: string | null
  required_secrets?: string[]
  ocsf_mapping_version?: string
  /** spec kinds aceitos pela fonte, ex. ["passthrough","sigma"]. */
  spec_kinds?: string[]
  /** plataformas que suportam este dialeto. */
  supported_by?: string[]
}

export type QueryJobStatus = "submitted" | "running" | "finished" | "partial" | "failed"

/** Resultado por-fonte dentro de um job federado. */
export interface QueryJobSourceRead {
  integration_id: number
  status: string
  count: number
  error?: string | null
  partial: boolean
}

/** Job de query federada. Resposta de GET /query-jobs/{job_id}. */
export interface QueryJobRead {
  job_id: string
  status: QueryJobStatus
  dialect: QueryDialect
  organization_id?: number
  integration_ids: number[]
  statement: string
  spec_kind?: QuerySpecKind
  from_ts: string
  to_ts: string
  allow_partial_results?: boolean
  total_results: number
  per_source: QueryJobSourceRead[]
  error_message?: string | null
  created_at?: string
  finished_at?: string | null
}

/** Payload de POST /query-jobs (202 → QueryJobRead com status submitted). */
export interface QueryJobSubmitRequest {
  integration_ids: number[]
  statement: string
  from_ts: string
  to_ts: string
  dialect?: QueryDialect
  allow_partial_results?: boolean
  spec_kind?: QuerySpecKind
}

export type DetectionSource = "scheduled_query" | "live_query" | "correlation"
export type DetectionStatus = "open" | "ack" | "closed"

/** Detecção de 1ª classe. NÃO confundir com Alert (coleta). */
export interface DetectionRead {
  id: number
  organization_id: number
  source: DetectionSource
  source_query_id?: number | null
  integration_id?: number | null
  dialect?: QueryDialect | null
  rule_id?: string | null
  rule_name?: string | null
  /** severidade OCSF (1=Informational … 6=Fatal); default 4 (High). */
  severity_id: number
  status: DetectionStatus
  dedup_key: string
  count?: number
  suppression_window_seconds?: number
  first_seen?: string | null
  last_seen?: string | null
  search_result_id?: number | null
  ocsf_ref?: string | null
  created_at?: string
}

/** Payload de PATCH /detections/{id}. */
export interface DetectionStatusUpdate {
  status: DetectionStatus
}

export type WhereOp = "eq" | "ne" | "contains" | "gt" | "lt" | "gte" | "lte"

/** Filtro de uma regra de correlação (where_json). */
export interface WhereFilter {
  field: string
  op: WhereOp
  value: string
}

/** Regra de correlação cross-source. rule_type='threshold' (MVP). */
export interface CorrelationRuleRead {
  id: number
  organization_id: number
  name: string
  description?: string | null
  enabled: boolean
  severity_id: number
  rule_type: string
  group_by_field?: string | null
  min_count: number
  window_seconds: number
  timestamp_field?: string | null
  where: WhereFilter[]
  suppression_window_seconds?: number
  created_at?: string
}

export interface CorrelationRuleCreate {
  name: string
  description?: string
  enabled?: boolean
  severity_id?: number
  group_by_field: string
  min_count?: number
  window_seconds?: number
  timestamp_field?: string
  where?: WhereFilter[]
  suppression_window_seconds?: number
  organization_id?: number
}

export interface CorrelationRuleUpdate {
  name?: string
  description?: string
  enabled?: boolean
  severity_id?: number
  group_by_field?: string
  min_count?: number
  window_seconds?: number
  timestamp_field?: string
  where?: WhereFilter[]
  suppression_window_seconds?: number
}

// ── OCSF governance ────────────────────────────────────────────
export type OcsfEnforcementMode = "tag_and_pass" | "quarantine" | "fail_closed"

export interface OcsfPolicy {
  organization_id: number
  organization_name: string | null
  enforcement_mode: OcsfEnforcementMode
  is_default: boolean
}

export interface OcsfComplianceItem {
  integration_id: number
  integration_name: string | null
  organization_id: number | null
  enforcement_mode: OcsfEnforcementMode
  invalid_quarantined_24h: number
}

export interface OcsfCompliance {
  validation_enabled: boolean
  global_default: OcsfEnforcementMode
  ocsf_version: string
  items: OcsfComplianceItem[]
}
