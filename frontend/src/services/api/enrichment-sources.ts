import { apiRequest } from "./_core"
import type { JsonSchema } from "@/types"

// ── Enriquecimento em stream (ADR-LOCAL-0002) ──────────────────────────────
// O catálogo é lido do registry do backend: adicionar uma fonte de
// enriquecimento NÃO toca o frontend.


export interface EnricherCatalogItem {
  name: string
  label: string
  category: string
  description: string
  icon_id: string | null
  docs_url: string | null
  tier: string
  order: number
  mode: "local" | "remote"
  key_kinds: string[]
  supports_bulk: boolean
  suggested_ttl_s: number
  license: string
  /** "none" | "internal" | "third_party" — consentimento de privacidade. */
  egress: "none" | "internal" | "third_party"
  required_secrets: string[]
  output_fields: Record<string, string>
  /** `model_json_schema` do enricher — dirige o formulário da fonte configurada. */
  config_schema?: JsonSchema | null
}

/** Instância configurada de um enricher, escopada à organização. */
export interface EnrichmentSource {
  id: string
  organization_id: number
  name: string
  enricher: string
  description: string | null
  config: Record<string, unknown>
  /** Booleano — a API NUNCA devolve a referência do segredo. */
  secret_configured: boolean
  enabled: boolean
  /** Filhas que também usam esta fonte (MSP). Vazio = só a dona. */
  shared_organization_ids: number[]
  /**
   * Veredito da última sondagem. `null` em `last_test_at` significa NUNCA
   * TESTADA, que é diferente de "testada e falhou": a primeira é um aviso, a
   * segunda traz a mensagem do provedor e pede ação imediata.
   */
  last_test_at?: string | null
  last_test_ok?: boolean | null
  last_test_message?: string | null
}

export interface EnrichmentSourceCreateRequest {
  name: string
  enricher: string
  organization_id: number
  description?: string | null
  config?: Record<string, unknown>
  /** Write-only: trafega em claro UMA vez; o servidor cifra e nunca devolve. */
  secret?: string | null
  enabled?: boolean
  shared_organization_ids?: number[]
}

export interface EnrichmentSourceUpdateRequest {
  description?: string | null
  config?: Record<string, unknown>
  /** `undefined` mantém o segredo; `""` remove; string nova substitui. */
  secret?: string | null
  enabled?: boolean
  shared_organization_ids?: number[]
}

export async function listEnrichmentSources() {
  return apiRequest<EnrichmentSource[]>("/collectors/enrichment/sources")
}

export async function createEnrichmentSource(data: EnrichmentSourceCreateRequest) {
  return apiRequest<EnrichmentSource>("/collectors/enrichment/sources", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function updateEnrichmentSource(
  id: string,
  data: EnrichmentSourceUpdateRequest,
) {
  return apiRequest<EnrichmentSource>(`/collectors/enrichment/sources/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  })
}

export interface EnrichmentSourceTestResult {
  ok: boolean
  message: string
  sample_count?: number | null
  sample?: Record<string, unknown> | null
  elapsed_ms?: number | null
}

/**
 * Sonda uma fonte que AINDA NÃO EXISTE, com o que está no formulário.
 *
 * Nada é gravado — nem a fonte, nem o veredito. Sem `secret`, mas com
 * `source_id`, o servidor usa a credencial já salva, para o operador testar
 * sem redigitar a chave ao editar outro campo.
 */
export async function testEnrichmentSourceDraft(data: {
  enricher: string
  organization_id?: number | null
  config?: Record<string, unknown>
  secret?: string
  source_id?: string
}) {
  return apiRequest<EnrichmentSourceTestResult>(
    "/collectors/enrichment/sources/test-draft",
    { method: "POST", body: JSON.stringify(data) },
  )
}

/** Sonda a fonte de verdade (1 página curta). Não persiste nada. */
export async function testEnrichmentSource(id: string) {
  return apiRequest<EnrichmentSourceTestResult>(
    `/collectors/enrichment/sources/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  )
}

export async function deleteEnrichmentSource(id: string) {
  return apiRequest<void>(`/collectors/enrichment/sources/${encodeURIComponent(id)}`, { method: "DELETE" })
}

