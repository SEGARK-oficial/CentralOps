import i18n from "@/i18n"
import { ADMIN_REDIRECT_PATH, ApiRequestError, BASE_URL, apiRequest } from "./_core"
import type {
  CaptureEventList,
  CaptureSession,
  CaptureSessionList,
  CaptureSessionStartRequest,
  CaptureTrajectory,
} from "@/types"

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

export async function getCaptureEvents(
  sessionId: string,
  limit = 200,
  orgId?: number | null,
  filters?: { outcome?: string; stage?: string },
) {
  // Os filtros são SERVER-SIDE. Filtrar no cliente fazia o export divergir da
  // tela: o operador via 12 eventos dropados e baixava o ring inteiro.
  const extra = new URLSearchParams()
  if (filters?.outcome) extra.set("outcome", filters.outcome)
  if (filters?.stage) extra.set("stage", filters.stage)
  const qs = extra.toString()
  return apiRequest<CaptureEventList>(
    `/collectors/config/capture-sessions/${encodeURIComponent(sessionId)}/events?limit=${limit}${captureOrgQuery(orgId, "&")}${qs ? `&${qs}` : ""}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

/** Trajetória de UM evento: todos os estágios, em ordem de pipeline.
 *
 *  Existe porque o ring é uma lista FIFO única compartilhada por todos os
 *  estágios e destinos — com fan-out N, as entradas de um evento vêm
 *  intercaladas com as de todos os outros, e juntar por página degrada
 *  exatamente no volume em que a junção é necessária. */
export async function getCaptureTrajectory(
  sessionId: string,
  eventId: string,
  orgId?: number | null,
) {
  return apiRequest<CaptureTrajectory>(
    `/collectors/config/capture-sessions/${encodeURIComponent(sessionId)}/events/${encodeURIComponent(eventId)}${captureOrgQuery(orgId, "?")}`,
    { forbiddenRedirectTo: ADMIN_REDIRECT_PATH },
  )
}

/** Constrói a URL de export (planilha CSV ou NDJSON) de uma sessão de captura.
 *  Usada num <a download> / window.open — o backend faz streaming e a máscara de
 *  PII (mask=true por default). */
export function captureExportUrl(
  sessionId: string,
  fmt: "csv" | "ndjson" = "csv",
  orgId?: number | null,
  opts?: { mask?: boolean; outcome?: string; stage?: string },
): string {
  const qs = new URLSearchParams({ fmt })
  // `mask` só vai explícito quando FALSO: o default do backend é mascarar, e
  // mandar `mask=true` sempre esconderia no log de auditoria a diferença entre
  // "o operador aceitou o default" e "o operador pediu mascarado".
  if (opts?.mask === false) qs.set("mask", "false")
  if (opts?.outcome) qs.set("outcome", opts.outcome)
  if (opts?.stage) qs.set("stage", opts.stage)
  return `${BASE_URL}/collectors/config/capture-sessions/${encodeURIComponent(sessionId)}/export?${qs.toString()}${captureOrgQuery(orgId, "&")}`
}

/** Baixa o export como arquivo. Fetch com credenciais (o download precisa do
 *  cookie de sessão) → blob → clique programático num link temporário. */
export async function downloadCaptureExport(
  sessionId: string,
  fmt: "csv" | "ndjson",
  orgId?: number | null,
  opts?: { mask?: boolean; outcome?: string; stage?: string },
): Promise<void> {
  const res = await fetch(captureExportUrl(sessionId, fmt, orgId, opts), {
    credentials: "include",
    headers: { "Accept-Language": i18n.language || "pt" },
  })
  if (!res.ok) throw new ApiRequestError(`export failed (${res.status})`, res.status)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement("a")
    a.href = url
    a.download = `capture-${sessionId}.${fmt}`
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
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

