import { apiRequest } from "./_core"
import { type EnrichmentPolicy, type EnrichmentTable } from "./enrichment-infra"
import { type EnricherCatalogItem } from "./enrichment-sources"

// ── Modelo da matriz (Enterprise) ───────────────────────────────────────────

export interface EnrichmentTemplateTarget {
  organization_id: number
  organization_name?: string | null
  /** `ready` | `blocked` | `overridden` | `up_to_date` | `applied` */
  status: string
  policy_id?: string | null
  policy_name?: string | null
  applied_version_id?: string | null
  missing_tables: string[]
  missing_sources: string[]
  tables_without_version: string[]
  /** Nome da política PRÓPRIA que vence o modelo, quando `overridden`. */
  overriding_policy?: string | null
}

export interface EnrichmentTemplatePreflight {
  template_policy_id: string
  template_version_id?: string | null
  targets: EnrichmentTemplateTarget[]
}

export interface EnrichmentTemplateApplyResult {
  applied: EnrichmentTemplateTarget[]
  skipped: EnrichmentTemplateTarget[]
}

/** Marca (ou desmarca) a política como modelo da matriz. */
export async function setEnrichmentPolicyTemplate(policyId: string, isTemplate: boolean) {
  return apiRequest<EnrichmentPolicy>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/template?is_template=${isTemplate}`,
    { method: "POST" },
  )
}

/** O que aconteceria em CADA filha. Não muda nada. */
export async function preflightEnrichmentTemplate(policyId: string) {
  return apiRequest<EnrichmentTemplatePreflight>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/template-preflight`,
    { method: "POST" },
  )
}

/**
 * Publica uma versão derivada em cada filha escolhida.
 *
 * A decisão é recalculada no servidor: o que estava bloqueado entre a tela e o
 * clique volta em `skipped`, e nada é escrito lá.
 */
export async function applyEnrichmentTemplate(
  policyId: string,
  data: { organization_ids: number[]; commit_message?: string },
) {
  return apiRequest<EnrichmentTemplateApplyResult>(
    `/collectors/enrichment/policies/${encodeURIComponent(policyId)}/apply-template`,
    { method: "POST", body: JSON.stringify(data) },
  )
}

export async function listEnrichers() {
  return apiRequest<EnricherCatalogItem[]>("/collectors/enrichment/enrichers")
}

export async function listEnrichmentTables() {
  return apiRequest<EnrichmentTable[]>("/collectors/enrichment/tables")
}

export async function listEnrichmentPolicies() {
  return apiRequest<EnrichmentPolicy[]>("/collectors/enrichment/policies")
}

// ── Tabelas: escrita ────────────────────────────────────────────────────────

export interface EnrichmentTableCreateRequest {
  name: string
  organization_id?: number | null
  description?: string | null
  match_mode: "exact" | "cidr"
  key_kind: string
}

export interface EnrichmentTableVersion {
  id: string
  version_number: number
  entry_count: number
  approx_bytes: number
  commit_message: string
  author_user_id: number | null
  created_at: string | null
  is_current: boolean
  invalid_rows: number
}

export async function createEnrichmentTable(data: EnrichmentTableCreateRequest) {
  return apiRequest<EnrichmentTable>("/collectors/enrichment/tables", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function deleteEnrichmentTable(id: string) {
  return apiRequest<void>(`/collectors/enrichment/tables/${encodeURIComponent(id)}`, { method: "DELETE" })
}

export async function listEnrichmentTableVersions(tableId: string) {
  return apiRequest<EnrichmentTableVersion[]>(`/collectors/enrichment/tables/${encodeURIComponent(tableId)}/versions`)
}

export interface EnrichmentTableVersionDetail {
  id: string
  version_number: number
  entry_count: number
  approx_bytes: number
  /** Corpo `{chave: {campo: valor}}` como foi gravado. */
  rows: Record<string, Record<string, unknown>>
}

/**
 * Conteúdo de UMA versão. A listagem devolve metadado; o corpo só vem por aqui,
 * e é o que permite diferenciar o arquivo novo contra o que está valendo antes
 * de publicar (publicar substitui a versão inteira).
 */
export async function getEnrichmentTableVersion(tableId: string, versionId: string) {
  return apiRequest<EnrichmentTableVersionDetail>(
    `/collectors/enrichment/tables/${encodeURIComponent(tableId)}/versions/${encodeURIComponent(versionId)}`,
  )
}

export async function commitEnrichmentTableVersion(
  tableId: string,
  data: { rows: Record<string, Record<string, unknown>>; commit_message: string },
) {
  return apiRequest<EnrichmentTableVersion>(`/collectors/enrichment/tables/${encodeURIComponent(tableId)}/versions`, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function rollbackEnrichmentTable(tableId: string, versionId: string) {
  return apiRequest<EnrichmentTable>(`/collectors/enrichment/tables/${encodeURIComponent(tableId)}/rollback`, {
    method: "POST",
    body: JSON.stringify({ version_id: versionId }),
  })
}

