import { apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type {
  DriftEntry,
  TypeCastDescriptor,
} from "@/types"

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
  return apiRequest<DriftEntry>(`/drift/${encodeURIComponent(id)}/ignore`, { method: "POST", ...options })
}

export async function markDriftMapped(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<DriftEntry> {
  return apiRequest<DriftEntry>(`/drift/${encodeURIComponent(id)}/mark_mapped`, { method: "POST", ...options })
}

export async function deleteDrift(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<void> {
  return apiRequest<void>(`/drift/${encodeURIComponent(id)}`, { method: "DELETE", ...options })
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

/** Um caminho do inventário de campos da organização (`GET /mappings/key-sources`). */
export interface MappingKeySource {
  path: string
  rule_count: number
  vendors: string[]
  /**
   * `mapped` = a org produz de fato (mappings ativos); `catalog` = ainda não
   * conectou nada e isto é o catálogo OCSF comum; `envelope` = rótulo
   * `_centralops.*` que o roteamento aceita em condição.
   */
  kind: "mapped" | "catalog" | "envelope"
}

export interface MappingKeySourcesResponse {
  organization_id: number
  from_active_mappings: boolean
  /** Raízes válidas de um path sobre o envelope (`_centralops` | `normalized` | `raw`). */
  roots: string[]
  suggestions: MappingKeySource[]
}

/**
 * GET /mappings/key-sources — caminhos que a organização de fato produz, para
 * quem escreve regra sobre o envelope (correlação em voo: `group_by_field` e
 * `where.field`). Mesma lista do enriquecimento, com permissão de LEITURA de
 * mapping. Sem `organization_id` o backend usa a org do usuário; um global
 * precisa nomear a org (422).
 */
export async function listMappingKeySources(
  params: { organization_id?: number } = {},
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<MappingKeySourcesResponse> {
  const qs = new URLSearchParams()
  if (params.organization_id != null) qs.set("organization_id", String(params.organization_id))
  const suffix = qs.toString() ? `?${qs.toString()}` : ""
  return apiRequest<MappingKeySourcesResponse>(`/mappings/key-sources${suffix}`, options)
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
  return apiRequest<DiscoverFieldsResponse>(`/mappings/${encodeURIComponent(mappingId)}/discover-fields`, options)
}

