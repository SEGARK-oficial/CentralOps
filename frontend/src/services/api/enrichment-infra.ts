import { apiRequest } from "./_core"

// ── Prontidão e configuração de infraestrutura ──────────────────────────────

export interface EnrichmentReadinessAction {
  label: string
  route: string
  /** `global` marca o que um admin de organização não resolve sozinho. */
  scope: "org" | "global"
}

export interface EnrichmentReadinessStep {
  key: string
  status: "ok" | "warning" | "blocked" | "not_applicable"
  title: string
  detail: string
  blocking: boolean
  action?: EnrichmentReadinessAction | null
}

export interface EnrichmentReadiness {
  organization_id: number
  ready: boolean
  steps: EnrichmentReadinessStep[]
  active_policy_name?: string | null
}

/** "Está funcionando aqui, e o que falta?" — a resposta em quatro passos. */
export interface EnrichmentDuplicatePreflight {
  target_organization_id: number
  ok: boolean
  missing_tables: string[]
  missing_sources: string[]
  /** Existe no destino, mas sem versão publicada. Avisa, não bloqueia. */
  tables_without_version: string[]
  name_conflict: boolean
}

export interface EnrichmentDuplicateRequest {
  target_organization_id: number
  name?: string
  commit_message?: string
}

/** Diz o que falta no destino ANTES de copiar. Não muda nada. */
export async function preflightDuplicateEnrichmentPolicy(
  policyId: string,
  data: EnrichmentDuplicateRequest,
) {
  return apiRequest<EnrichmentDuplicatePreflight>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/duplicate-preflight`,
    { method: "POST", body: JSON.stringify(data) },
  )
}

/**
 * Copia as regras para outra organização. A cópia nasce DESABILITADA: colocar
 * regra no caminho quente de outro tenant é decisão de quem opera aquele
 * tenant.
 */
export async function duplicateEnrichmentPolicy(
  policyId: string,
  data: EnrichmentDuplicateRequest,
) {
  return apiRequest<EnrichmentPolicy>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/duplicate`,
    { method: "POST", body: JSON.stringify(data) },
  )
}

export async function getEnrichmentReadiness(params: { organization_id?: number } = {}) {
  const qs = params.organization_id != null ? `?organization_id=${params.organization_id}` : ""
  return apiRequest<EnrichmentReadiness>(`/collectors/enrichment/readiness${qs}`)
}

export interface EnrichmentGeoipFile {
  name: string
  size_bytes: number
  modified_at: number
}

export interface EnrichmentConfig {
  is_persisted: boolean
  config_version: string
  enabled: boolean
  redis_host: string | null
  redis_port: number
  redis_db: number
  redis_use_tls: boolean
  /** Booleano — a senha nunca volta pela API. */
  redis_secret_configured: boolean
  redis_url_masked: string | null
  /** `false` ⇒ enrichers por lote desligados em TODAS as organizações. */
  redis_configured: boolean
  remote_batch_budget_ms: number
  cycle_budget_ms: number
  l1_max_entries: number
  singleflight_wait_ms: number
  breaker_failure_threshold: number
  breaker_window_s: number
  breaker_cooldown_s: number
  breaker_max_cooldown_s: number
  max_table_bytes: number
  lru_bytes: number
  /** Enrichers do catálogo que param sem o cache L2. Vem do registry. */
  remote_enrichers: string[]
  propagation_worst_case_s: number
  geoip_dir: string | null
  geoip_files: EnrichmentGeoipFile[]
  updated_at?: string | null
}

/**
 * Update PARCIAL. `redis_password` tem três estados e a diferença destrói dado
 * se ignorada: ausente MANTÉM o segredo, string vazia REMOVE, string com
 * conteúdo substitui.
 */
export type EnrichmentConfigUpdateRequest = Partial<
  Pick<
    EnrichmentConfig,
    | "enabled"
    | "redis_host"
    | "redis_port"
    | "redis_db"
    | "redis_use_tls"
    | "remote_batch_budget_ms"
    | "cycle_budget_ms"
    | "l1_max_entries"
    | "singleflight_wait_ms"
    | "breaker_failure_threshold"
    | "breaker_window_s"
    | "breaker_cooldown_s"
    | "breaker_max_cooldown_s"
    | "max_table_bytes"
    | "lru_bytes"
  >
> & { redis_password?: string }

export async function getEnrichmentConfig() {
  return apiRequest<EnrichmentConfig>("/collectors/enrichment/config")
}

export async function updateEnrichmentConfig(data: EnrichmentConfigUpdateRequest) {
  return apiRequest<EnrichmentConfig>("/collectors/enrichment/config", {
    method: "PUT",
    body: JSON.stringify(data),
  })
}

export interface EnrichmentRedisTestResult {
  ok: boolean
  message: string
  latency_ms?: number | null
  maxmemory_policy?: string | null
  maxmemory_bytes?: number | null
  /** `null` = não deu para confirmar; `false` = É a instância principal. */
  distinct_from_main?: boolean | null
  warnings: string[]
}

/** Sonda com valores de RASCUNHO. Nada é persistido. */
export async function testEnrichmentRedis(data: {
  redis_host: string
  redis_port: number
  redis_db: number
  redis_use_tls: boolean
  redis_password?: string
}) {
  return apiRequest<EnrichmentRedisTestResult>(
    "/collectors/enrichment/config/test-redis",
    { method: "POST", body: JSON.stringify(data) },
  )
}

export interface EnrichmentTable {
  id: string
  organization_id: number
  name: string
  description: string | null
  match_mode: "exact" | "cidr"
  key_kind: string
  current_version_id: string | null
  entry_count: number
  approx_bytes: number
}

export interface EnrichmentPolicy {
  id: string
  organization_id: number
  name: string
  description: string | null
  enabled: boolean
  current_version_id: string | null
  rule_count: number
  /** A política que o worker aplica nesta org: só uma por organização. */
  is_active?: boolean
  /** Modelo da matriz (Enterprise). Não muda nada no runtime por si só. */
  is_template?: boolean
  /** Versão do modelo que originou a versão vigente, quando herdada. */
  derived_from_version_id?: string | null
}

