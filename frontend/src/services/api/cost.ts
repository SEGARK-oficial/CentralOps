import { apiRequest } from "./_core"

// ── resumo de volume/redução/custo ────────────────────────
export interface CostSummaryRow {
  organization_id: number
  bytes_in: number
  bytes_out: number
  events_in: number
  events_out: number
  out_in_byte_ratio: number | null
  reduction_active: boolean
  bytes_saved: number
  /** Decomposição de bytes_saved por causa (trim/sample/suppress/drop/…).
   *  Causas que não dispararam são omitidas pelo backend. */
  bytes_saved_by_reason: Record<string, number>
  /** saved / (out + saved) — denominador CONTRAFACTUAL, não bytes_in. */
  reduction_pct: number | null
  /** true quando bytes_saved > bytes_in: funil impossível causado por bases de
   *  medição diferentes (ver CostSummary.units), não por dupla contagem. */
  unit_mismatch: boolean
  savings_usd_per_day: number | null
  cost: { usd: number; currency: string } | null
}
export interface CostSummary {
  window_minutes: number
  enabled: boolean
  pricing_available: boolean
  /** Pricer EE registrado (o pacote está presente) mas a licença Enterprise não
   *  está ativa — o bloco US$ é omitido por LICENÇA, não por falta de preço. */
  pricing_license_required?: boolean
  /** Estado real das flags REDUCTION_* no backend que respondeu. */
  levers: Record<string, boolean>
  /** Base de medição de cada métrica (`raw_event`, `envelope_per_delivery`, …). */
  units: Record<string, string>
  rows: CostSummaryRow[]
  note: string
}

/** Volume ingerido vs entregue + economia por-org. O bloco US$ só vem quando
 *  o pacote Enterprise registra um pricer (Community devolve só volume + % de redução). */
export async function getCostSummary(): Promise<CostSummary> {
  return apiRequest<CostSummary>(`/collectors/cost-summary`)
}

