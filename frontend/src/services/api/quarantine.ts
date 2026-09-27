import { apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type {
  QuarantineDetail,
  QuarantineEntry,
} from "@/types"

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
  return apiRequest<QuarantineDetail>(`/quarantine/${encodeURIComponent(id)}`, options)
}

export async function discardQuarantine(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<void> {
  return apiRequest<void>(`/quarantine/${encodeURIComponent(id)}/discard`, { method: "POST", ...options })
}

export async function reprocessQuarantine(
  id: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<QuarantineEntry> {
  return apiRequest<QuarantineEntry>(`/quarantine/${encodeURIComponent(id)}/reprocess`, { method: "POST", ...options })
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

