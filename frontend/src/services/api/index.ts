/**
 * api/index — barrel do serviço de API (R4-6.3).
 *
 * Preserva EXATAMENTE a superfície pública que `src/services/api.ts` tinha
 * antes da divisão por domínio: todo named export que já existia continua
 * existindo aqui, com o mesmo nome. `apiRequest`/`ApiRequestOptions` (infra
 * interna de `_core.ts`) DELIBERADAMENTE não são re-exportados — não eram
 * públicos no arquivo único.
 */
export { ApiRequestError, formatValidationDetail } from "./_core"
export * from "./auth"
export * from "./account"
export * from "./users"
export * from "./search-history"
export * from "./queries"
export * from "./schedules"
export * from "./emails"
export * from "./dashboard"
export * from "./organizations"
export * from "./integrations"
export * from "./collectors"
export * from "./edition"
export * from "./capture"
export * from "./mappings"
export * from "./drift"
export * from "./quarantine"
export * from "./pipeline-health"
export * from "./backfill"
export * from "./tokens"
export * from "./service-accounts"
export * from "./destinations"
export * from "./ingest"
export * from "./cost"
export * from "./routes"
export * from "./query-jobs"
export * from "./correlation"
export * from "./ocsf"
export * from "./enrichment-sources"
export * from "./enrichment-infra"
export * from "./enrichment-tables"
export * from "./enrichment-policies"
