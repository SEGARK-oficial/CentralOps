import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type {
  Destination,
  DestinationCreateRequest,
  DestinationHealth,
  DestinationShadowResult,
  DestinationTestResult,
  DestinationType,
  DestinationUpdateRequest,
} from "@/types"

// ── Destinos (saída multi-destino) ───────────────────────────


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

