import { apiRequest } from "./_core"
import type {
  CorrelationLimitsRead,
  CorrelationRuleCreate,
  CorrelationRuleMetricsRead,
  CorrelationRulePreviewRequest,
  CorrelationRulePreviewResult,
  CorrelationRuleRead,
  CorrelationRuleUpdate,
  DetectionRead,
  DetectionStatusUpdate,
} from "@/types"

const DETECTIONS_BASE = "/detections"
const CORRELATION_RULES_BASE = "/correlation-rules"

export async function listDetections(params?: { status_filter?: DetectionStatusUpdate["status"]; limit?: number }) {
  const q = new URLSearchParams()
  if (params?.status_filter) q.set("status_filter", params.status_filter)
  if (params?.limit) q.set("limit", String(params.limit))
  const qs = q.toString()
  return apiRequest<DetectionRead[]>(`${DETECTIONS_BASE}${qs ? `?${qs}` : ""}`)
}

export async function getDetection(id: number) {
  return apiRequest<DetectionRead>(`${DETECTIONS_BASE}/${id}`)
}

/** Triagem: muda o status de uma detecção (open→ack→closed). QUERY_RUN. */
export async function updateDetectionStatus(id: number, payload: DetectionStatusUpdate) {
  return apiRequest<DetectionRead>(`${DETECTIONS_BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  })
}

/**
 * Lista regras de correlação org-scoped. QUERY_RUN.
 *
 * `include_inflight_status` é OPT-IN e não default: ele custa um COUNT e uma
 * compilação por organização presente na resposta. Sem ele o backend devolve
 * `not_evaluated_inflight: false` em todas — e esse `false` é "não calculado",
 * não "está rodando". Quem for renderizar o aviso PRECISA pedir o cálculo.
 */
export async function listCorrelationRules(params: { include_inflight_status?: boolean } = {}) {
  const qs = new URLSearchParams()
  if (params.include_inflight_status) qs.set("include_inflight_status", "true")
  const query = qs.toString()
  return apiRequest<CorrelationRuleRead[]>(`${CORRELATION_RULES_BASE}${query ? `?${query}` : ""}`)
}

/**
 * Tetos e truncamento da avaliação em voo, para UMA org (ADR-0015). QUERY_RUN.
 *
 * Sem `organization_id` o backend assume a org do usuário; um usuário global
 * SEM org recebe 400 em vez de uma agregação cross-tenant — os tetos são por
 * org e somá-los produziria um número que não descreve organização nenhuma.
 */
export async function getCorrelationLimits(params: { organization_id?: number } = {}) {
  const qs = new URLSearchParams()
  if (params.organization_id != null) qs.set("organization_id", String(params.organization_id))
  const query = qs.toString()
  return apiRequest<CorrelationLimitsRead>(`${CORRELATION_RULES_BASE}/limits${query ? `?${query}` : ""}`)
}

/**
 * Disparos, overflow e erros de UMA regra na janela (ADR-0015). QUERY_RUN.
 *
 * `range_minutes` é limitado no backend pela retenção da série (24h): pedir
 * mais devolveria a parte expirada como zero, indistinguível de "não disparou".
 * Por isso o default daqui é o do backend — omitir é mais seguro que chutar.
 *
 * Toda métrica volta `number | null`, e `null` NÃO é 0: ver
 * `CorrelationRuleMetricsRead`. Renderizar os dois igual apaga a diferença
 * entre "não disparou" e "não consegui ler".
 */
export async function getCorrelationRuleMetrics(
  ruleId: number,
  params: { range_minutes?: number } = {},
) {
  const qs = new URLSearchParams()
  if (params.range_minutes != null) qs.set("range_minutes", String(params.range_minutes))
  const query = qs.toString()
  return apiRequest<CorrelationRuleMetricsRead>(
    `${CORRELATION_RULES_BASE}/${ruleId}/metrics${query ? `?${query}` : ""}`,
  )
}

export async function getCorrelationRule(id: number) {
  return apiRequest<CorrelationRuleRead>(`${CORRELATION_RULES_BASE}/${id}`)
}

/** Cria regra de correlação (201). QUERY_SAVE. 409 = quota por org estourada. */
export async function createCorrelationRule(data: CorrelationRuleCreate) {
  return apiRequest<CorrelationRuleRead>(`${CORRELATION_RULES_BASE}`, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function updateCorrelationRule(id: number, data: CorrelationRuleUpdate) {
  return apiRequest<CorrelationRuleRead>(`${CORRELATION_RULES_BASE}/${id}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  })
}

export async function deleteCorrelationRule(id: number) {
  return apiRequest<void>(`${CORRELATION_RULES_BASE}/${id}`, {
    method: "DELETE",
  })
}

/**
 * Testa uma regra (ainda não salva) contra o reservoir de amostras reais de
 * `vendor`+`event_type` (ADR-0015, Fase 3). NUNCA persiste nada — cada chamada
 * abre uma conexão Redis nova no backend, por isso o caller (UI) só deve
 * disparar isto num clique explícito, nunca em auto-run por keystroke.
 */
export async function previewCorrelationRule(data: CorrelationRulePreviewRequest) {
  return apiRequest<CorrelationRulePreviewResult>(`${CORRELATION_RULES_BASE}/preview`, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

