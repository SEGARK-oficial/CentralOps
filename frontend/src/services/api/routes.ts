import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"

// R4-6.3: mesmo valor de `destinations.ts` (`DEST_BASE`) — as funções abaixo
// (DLQ, credencial, lineage) operam em endpoints de destino, mas o const
// original não era `export`; duplicar a string aqui evita virar export
// público novo do barrel só para compartilhar entre 2 arquivos-irmãos.
const DEST_BASE = "/collectors/destinations"
import type {
  ConfigBundle,
  ConfigImportRequest,
  ConfigImportResponse,
  CredentialAuditResponse,
  CredentialRevokeResponse,
  CredentialRotateRequest,
  CredentialRotateResponse,
  DestinationLineageResponse,
  DlqReprocessResponse,
  EventLineageResponse,
  Route,
  RouteAudit,
  RouteCreateRequest,
  RouteDryRunRequest,
  RouteDryRunResponse,
  RouteReorderResponse,
  RouteUpdateRequest,
} from "@/types"

// ── Rotas (motor de roteamento) ───────────────────────


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


/**
 * POST /collectors/routes/reorder
 * Reatribui prioridades em bulk na ordem fornecida (drag-and-drop).
 * O roteamento por regra é sempre-ativo (GA): a antiga ROUTING_ENABLED foi removida.
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

