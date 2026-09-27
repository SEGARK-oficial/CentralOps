import { apiRequest } from "./_core"
import { type EnrichmentPolicy } from "./enrichment-infra"

// ── Políticas: escrita ──────────────────────────────────────────────────────

export interface EnrichmentPolicyCreateRequest {
  name: string
  organization_id?: number | null
  description?: string | null
}

export interface EnrichmentPolicyVersion {
  id: string
  version_number: number
  commit_message: string
  author_user_id: number | null
  created_at: string | null
  is_current: boolean
  summary: {
    version: number
    rule_count: number
    has_local: boolean
    has_remote: boolean
    rules: Array<{
      id: string
      enricher: string
      table: string | null
      mode: string
      key: { source: string; kind: string }
      targets: string[]
      tags: string[]
      on_miss: string
      on_multi: string
      on_error: string
    }>
  } | null
}

/** Uma regra de política, no formato aceito por `POST .../policies/{id}/versions`. */
export interface EnrichmentRule {
  id: string
  enricher: string
  table?: string | null
  /** Nome da `EnrichmentSource` desta org. A credencial NUNCA vai na regra. */
  source?: string | null
  key: { source: string; kind: string; normalize?: string[] }
  when?: Record<string, unknown> | null
  outputs: Array<{ from: string; target: string; default?: unknown }>
  tags?: string[]
  on_miss?: "skip" | "default" | "tag"
  on_multi?: "first" | "error" | "array"
  on_error?: "skip" | "tag"
  overwrite?: boolean
  mode?: "local" | "remote"
  ttl_s?: number
  negative_ttl_s?: number
  entry_ttl_s?: number
}

export async function createEnrichmentPolicy(data: EnrichmentPolicyCreateRequest) {
  return apiRequest<EnrichmentPolicy>("/collectors/enrichment/policies", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function listEnrichmentPolicyVersions(policyId: string) {
  return apiRequest<EnrichmentPolicyVersion[]>(`/collectors/enrichment/policies/${encodeURIComponent(policyId)}/versions`)
}

/**
 * Regras CRUAS de uma versão, para carregar no editor.
 *
 * Não dá para editar a partir do `summary` da listagem: ele achata cada output
 * em `targets` (perde o `from`) e não carrega o `when`. Publicar em cima dele
 * apagaria a condição que estava na versão.
 */
export interface EnrichmentKeySourceSuggestion {
  path: string
  rule_count: number
  vendors: string[]
}

/**
 * Caminhos que a organização de fato produz, para o campo `key.source`.
 *
 * `from_active_mappings=false` significa que a org ainda não tem integração
 * ativa e a lista é o catálogo OCSF comum — a UI deve dizer isso, em vez de
 * apresentar um fallback estático como se fosse o inventário do cliente.
 */
export async function listEnrichmentKeySources(params: { organization_id?: number }) {
  const qs = new URLSearchParams()
  if (params.organization_id != null) qs.set("organization_id", String(params.organization_id))
  return apiRequest<{
    organization_id: number
    from_active_mappings: boolean
    suggestions: EnrichmentKeySourceSuggestion[]
  }>(`/collectors/enrichment/key-sources?${qs.toString()}`)
}

export async function getEnrichmentPolicyVersion(policyId: string, versionId: string) {
  return apiRequest<{ id: string; version_number: number; rules: EnrichmentRule[] }>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/versions/${encodeURIComponent(versionId)}`,
  )
}

export interface EnrichmentRuleMetrics {
  rule_id: string
  enricher?: string | null
  source?: string | null
  hit: number
  miss: number
  skipped: number
  error: number
}

export interface EnrichmentActivityEntry {
  ts: number
  kind: "table_load" | "remote_batch"
  ok: boolean
  rule_id?: string | null
  enricher?: string | null
  source?: string | null
  reason?: string | null
  detail?: string | null
  keys?: number | null
  entries?: number | null
  latency_ms?: number | null
}

/** Contadores por regra na janela. O teto de 180 min vem da retenção do store. */
export async function getEnrichmentMetrics(params: {
  organization_id?: number
  range_minutes?: number
}) {
  const qs = new URLSearchParams()
  if (params.organization_id != null) qs.set("organization_id", String(params.organization_id))
  if (params.range_minutes != null) qs.set("range_minutes", String(params.range_minutes))
  return apiRequest<{
    organization_id: number
    range_minutes: number
    /** Política efetivamente aplicada. `null` = nenhuma habilitada com versão. */
    policy_name?: string | null
    rules: EnrichmentRuleMetrics[]
  }>(`/collectors/enrichment/metrics?${qs.toString()}`)
}

/** Últimas tentativas de consulta (uma por ciclo ou por lote, nunca por evento). */
export async function getEnrichmentActivity(params: {
  organization_id?: number
  limit?: number
  kind?: string
  only_failures?: boolean
}) {
  const qs = new URLSearchParams()
  if (params.organization_id != null) qs.set("organization_id", String(params.organization_id))
  if (params.limit != null) qs.set("limit", String(params.limit))
  if (params.kind) qs.set("kind", params.kind)
  if (params.only_failures) qs.set("only_failures", "true")
  return apiRequest<{ organization_id: number; entries: EnrichmentActivityEntry[] }>(
    `/collectors/enrichment/activity?${qs.toString()}`,
  )
}

export async function commitEnrichmentPolicyVersion(
  policyId: string,
  data: { rules: EnrichmentRule[]; commit_message: string },
) {
  return apiRequest<EnrichmentPolicyVersion>(`/collectors/enrichment/policies/${encodeURIComponent(policyId)}/versions`, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function rollbackEnrichmentPolicy(policyId: string, versionId: string) {
  return apiRequest<EnrichmentPolicy>(`/collectors/enrichment/policies/${encodeURIComponent(policyId)}/rollback`, {
    method: "POST",
    body: JSON.stringify({ version_id: versionId }),
  })
}

export async function setEnrichmentPolicyEnabled(policyId: string, enabled: boolean) {
  return apiRequest<EnrichmentPolicy>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/enable?enabled=${enabled ? "true" : "false"}`,
    { method: "POST" },
  )
}

// ── Dry-run ──────────────────────────────────────────────────────────────────

export interface EnrichmentDryRunRequest {
  rules: EnrichmentRule[]
  sample: Record<string, unknown>
  tables?: Record<string, Record<string, unknown>>
}

export interface EnrichmentDryRunResponse {
  ok: boolean
  summary: EnrichmentPolicyVersion["summary"]
  enriched: Record<string, unknown>
  hits: Record<string, number>
  misses: Record<string, number>
  skipped: Record<string, number>
  errors: Record<string, number>
  bytes_added: number
}

export async function dryRunEnrichment(data: EnrichmentDryRunRequest) {
  return apiRequest<EnrichmentDryRunResponse>("/collectors/enrichment/dry-run", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

