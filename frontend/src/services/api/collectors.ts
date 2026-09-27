import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  CollectionState,
  CollectorConfig,
  CollectorSummary,
  CollectorTriggerResponse,
  CollectorVendor,
  UpdateCollectorConfigRequest,
} from "@/types"

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
    `/collectors/state/${encodeURIComponent(integrationId)}/${encodeURIComponent(stream)}/trigger`,
    { method: "POST" },
  )
}

export async function resetCollectorCursor(integrationId: number, stream: string) {
  return apiRequest<void>(
    `/collectors/state/${encodeURIComponent(integrationId)}/${encodeURIComponent(stream)}/cursor`,
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

