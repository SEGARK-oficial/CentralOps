import { apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type {
  DryRunResult,
  Mapping,
  MappingAuditEntry,
  MappingPayload,
  MappingRule,
  MappingVersion,
} from "@/types"

// ── Mapping API ──────────────────────────────────────────────────────


export interface MappingListItem {
  id: string
  vendor: string
  event_type: string
  description?: string | null
  current_version_id: string | null
  rules_count?: number | null
  created_at: string
  updated_at: string
}

export async function listMappings(
  options?: { include_rules_count?: boolean; only_active?: boolean; signal?: AbortSignal },
): Promise<MappingListItem[]> {
  const params = new URLSearchParams()
  if (options?.include_rules_count) {
    params.set("include_rules_count", "true")
  }
  // A política de "só integrações ativas" mora na UI: o default da API é permissivo,
  // então enviamos o valor explícito sempre que definido.
  if (typeof options?.only_active === "boolean") {
    params.set("only_active", String(options.only_active))
  }
  const qs = params.toString()
  return apiRequest<MappingListItem[]>(`/mappings${qs ? `?${qs}` : ""}`, {
    signal: options?.signal,
  })
}

export async function getMapping(id: string, options?: Pick<ApiRequestOptions, "signal">) {
  return apiRequest<Mapping & { versions: MappingVersion[] }>(`/mappings/${encodeURIComponent(id)}`, options)
}

export async function getMappingVersions(mappingId: string, options?: Pick<ApiRequestOptions, "signal">) {
  return apiRequest<MappingVersion[]>(`/mappings/${encodeURIComponent(mappingId)}/versions`, options)
}

export interface MappingSamplesResponse {
  vendor: string
  event_type: string
  total_in_reservoir: number
  items: Record<string, unknown>[]
}

/**
 * GET /mappings/samples — ring buffer de eventos brutos por tenant, no Redis,
 * alimentado pelo pipeline a cada evento pós-dedupe.
 *
 * Nunca devolve 404: reservoir vazio é lista vazia com `total_in_reservoir: 0`.
 *
 * `org_id` não é opcional por capricho. O reservoir é escopado por organização, e
 * um admin GLOBAL tem `organization_id = null`: sem o parâmetro explícito o backend
 * resolve a org efetiva como None e devolve vazio sem sequer consultar o Redis. O
 * chamador precisa nomear o tenant cujas amostras quer ver.
 */
export async function getMappingSamples(
  params: { vendor: string; event_type: string; limit?: number; org_id?: number | null },
  options?: Pick<ApiRequestOptions, "signal">,
) {
  const qs = new URLSearchParams({ vendor: params.vendor, event_type: params.event_type })
  qs.set("limit", String(params.limit ?? 10))
  if (params.org_id != null) qs.set("org_id", String(params.org_id))
  return apiRequest<MappingSamplesResponse>(`/mappings/samples?${qs.toString()}`, options)
}

export async function postMappingDryRun(
  payload: {
    /** Lista de regras (interno). Wrap em dict v2 antes de enviar. */
    rules: MappingRule[]
    /** Lista de ops de pré-processamento (default: vazia). */
    preprocess?: import("@/types").PreprocessOp[]
    raw_events?: Record<string, unknown>[]
    vendor?: string
    event_type?: string
    limit?: number
    /** admin global nomeia o tenant cujo reservoir inspecionar. */
    organization_id?: number
  },
  options?: Pick<ApiRequestOptions, "signal">,
) {
  const { rules, preprocess, ...rest } = payload
  const body = {
    ...rest,
    rules: { preprocess: preprocess ?? [], rules },
  }
  return apiRequest<DryRunResult>("/mappings/dry-run", {
    method: "POST",
    body: JSON.stringify(body),
    ...options,
  })
}

/**
 * Resposta paginada do endpoint de audit do backend.
 * Mantida exportada para os hooks que precisarem ler `total` no futuro.
 */
export interface MappingAuditListResponse {
  total: number
  items: MappingAuditEntry[]
  limit: number
  offset: number
  /**
   * Ações que ESTE endpoint pode devolver, servidas pelo backend
   * (`db/mapping_audit.DEFINITION_SCOPED_ACTIONS`). A UI monta o seletor de
   * filtro a partir daqui em vez de manter a própria cópia — a cópia anterior
   * oferecia três ações que o backend nunca grava e, como o filtro é igualdade
   * exata, escolhê-las devolvia tabela vazia sem erro.
   */
  available_actions?: string[]
}

export async function getMappingAudit(
  id: string,
  params?: {
    limit?: number
    offset?: number
    action?: string
    username?: string
    from_ts?: string
    to_ts?: string
  },
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<{ items: MappingAuditEntry[]; total: number; availableActions: string[] }> {
  const sp = new URLSearchParams()
  if (params?.limit) sp.set("limit", String(params.limit))
  if (params?.offset) sp.set("offset", String(params.offset))
  if (params?.action) sp.set("action", params.action)
  if (params?.username) sp.set("username", params.username)
  if (params?.from_ts) sp.set("from_ts", params.from_ts)
  if (params?.to_ts) sp.set("to_ts", params.to_ts)
  const qs = sp.toString()

  // Backend retorna envelope paginado {total, items, limit, offset}.
  // Defensivo: aceita tanto array direto (caso o backend mude) quanto envelope.
  const response = await apiRequest<MappingAuditListResponse | MappingAuditEntry[]>(
    `/mappings/${encodeURIComponent(id)}/audit${qs ? `?${qs}` : ""}`,
    options,
  )
  if (Array.isArray(response)) {
    return { items: response, total: response.length, availableActions: [] }
  }
  return {
    items: response?.items ?? [],
    total: response?.total ?? 0,
    // Backend anterior ao campo → lista vazia, e a UI cai no fallback estático.
    availableActions: response?.available_actions ?? [],
  }
}

// ── Sprint 2: criar versão, rollback, diff ────────────────────────────────────

export interface CreateMappingVersionRequest {
  /** Shape v2: { preprocess, rules }. Backend só aceita esse shape. */
  rules: MappingPayload
  commit_message: string
}

export interface RollbackMappingRequest {
  version_id: string
  commit_message: string
}

export async function createMappingVersion(
  mappingId: string,
  payload: CreateMappingVersionRequest,
) {
  return apiRequest<MappingVersion>(`/mappings/${encodeURIComponent(mappingId)}/versions`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function rollbackMapping(
  mappingId: string,
  payload: RollbackMappingRequest,
) {
  return apiRequest<MappingVersion>(`/mappings/${encodeURIComponent(mappingId)}/rollback`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export interface MappingVersionDiffResponse {
  definition_id: string
  version_a: string
  version_b: string
  version_a_number: number
  version_b_number: number
  reordered_only: boolean
  added: MappingRule[]
  removed: MappingRule[]
  modified: { target: string; before: MappingRule; after: MappingRule }[]
}

export async function getMappingDiff(
  mappingId: string,
  versionA: string,
  versionB: string,
  options?: Pick<ApiRequestOptions, "signal">,
) {
  return apiRequest<MappingVersionDiffResponse>(
    `/mappings/${encodeURIComponent(mappingId)}/versions/${encodeURIComponent(versionA)}/diff/${encodeURIComponent(versionB)}`,
    options,
  )
}

