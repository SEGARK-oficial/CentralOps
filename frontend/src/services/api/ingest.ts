import { apiRequest } from "./_core"

// ── Ingestão push (FortiGate/WEC) ────────────────────────────
export interface IngestInfo {
  integration_id: number
  platform: string
  transport: string
  streams: string[]
  has_token: boolean
  endpoint_base: string
  buffer_depth: number
  icon_id?: string | null
}

/** Metadados de ingestão. Lança (422) se a integração não é uma fonte push. */
export async function getIngestInfo(integrationId: number): Promise<IngestInfo> {
  return apiRequest<IngestInfo>(`/ingest/integrations/${encodeURIComponent(integrationId)}`)
}

/** Emite/rotaciona o token de ingestão. Devolve o token em claro UMA vez. */
export async function issueIngestToken(integrationId: number): Promise<{ token: string; endpoint: string }> {
  return apiRequest<{ token: string; endpoint: string }>(`/ingest/integrations/${encodeURIComponent(integrationId)}/token`, {
    method: "POST",
  })
}

// ── Receptor syslog nativo (fontes por CIDR + classificador) ──
export interface SyslogClassifierRule {
  when: string
  stream: string
}
export interface SyslogSource {
  id: number
  organization_id: number
  integration_id: number
  platform: string
  name: string
  source_cidr: string
  listen_port: number | null
  transport: "any" | "udp" | "tcp" | "tls"
  default_stream: string
  classifier: { rules: SyslogClassifierRule[] }
  enabled: boolean
  created_at: string
  updated_at: string
}
export interface SyslogSourceCreate {
  integration_id: number
  name: string
  source_cidr: string
  listen_port?: number | null
  transport?: SyslogSource["transport"]
  default_stream: string
  classifier?: { rules: SyslogClassifierRule[] }
  enabled?: boolean
}
export interface SyslogDetector {
  name: string
  label: string
  when: string
}
export interface SyslogClassifyTest {
  parsed: Record<string, unknown>
  stream: string | null
  matched_rule: boolean
  trace: Array<{ when: string; stream: string; result: unknown; matched: boolean }>
}

export async function listSyslogSources(integrationId: number): Promise<SyslogSource[]> {
  return apiRequest<SyslogSource[]>(`/syslog/sources?integration_id=${encodeURIComponent(integrationId)}`)
}
export async function createSyslogSource(payload: SyslogSourceCreate): Promise<SyslogSource> {
  return apiRequest<SyslogSource>("/syslog/sources", { method: "POST", body: JSON.stringify(payload) })
}
export async function updateSyslogSource(id: number, payload: Partial<SyslogSourceCreate>): Promise<SyslogSource> {
  return apiRequest<SyslogSource>(`/syslog/sources/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) })
}
export async function deleteSyslogSource(id: number): Promise<void> {
  await apiRequest<void>(`/syslog/sources/${encodeURIComponent(id)}`, { method: "DELETE" })
}
export async function listSyslogDetectors(): Promise<SyslogDetector[]> {
  return apiRequest<SyslogDetector[]>("/syslog/classifiers")
}
export async function testSyslogClassifier(payload: {
  line: string
  classifier?: { rules: SyslogClassifierRule[] }
  default_stream?: string
}): Promise<SyslogClassifyTest> {
  return apiRequest<SyslogClassifyTest>("/syslog/classify-test", { method: "POST", body: JSON.stringify(payload) })
}

// ── Streams da fonte genérica (custom_json) ──────────────────
export interface CustomStream {
  stream: string
  event_type: string
  definition_id: string
  ocsf_class_uid: number
  ocsf_class_name: string
  description: string | null
  current_version_id: string | null
  /** Caminho relativo do endpoint de ingestão deste stream. */
  endpoint: string
}

export interface CustomStreamCreate {
  stream: string
  ocsf_class_uid: number
  description?: string
}

/** Streams da plataforma custom_json (um por definição de mapping vendor=custom_json). */
export async function listCustomStreams(): Promise<CustomStream[]> {
  return apiRequest<CustomStream[]>("/mappings/custom-streams")
}

/** Cria um stream: definição custom_json.<stream> + versão 1 (esqueleto OCSF) promovida. */
export async function createCustomStream(payload: CustomStreamCreate): Promise<CustomStream> {
  return apiRequest<CustomStream>("/mappings/custom-streams", {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

/** Revoga o token de ingestão SEM rotacionar (mata um token vazado). 204/404. */
export async function revokeIngestToken(integrationId: number): Promise<void> {
  await apiRequest<void>(`/ingest/integrations/${encodeURIComponent(integrationId)}/token`, {
    method: "DELETE",
  })
}

