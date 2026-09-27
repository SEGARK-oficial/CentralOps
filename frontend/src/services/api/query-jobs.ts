import { apiRequest } from "./_core"
import type {
  QueryCapabilityRead,
  QueryJobRead,
  QueryJobSubmitRequest,
} from "@/types"

const QUERY_JOBS_BASE = "/query-jobs"

/** Catálogo de dialetos de query suportados (só exige autenticação). */
export async function listQueryCapabilities() {
  return apiRequest<QueryCapabilityRead[]>("/providers/query-capabilities")
}

/** Submete um job de query federada (202 → job com status submitted). QUERY_RUN. */
export async function submitQueryJob(data: QueryJobSubmitRequest) {
  return apiRequest<QueryJobRead>(QUERY_JOBS_BASE, {
    method: "POST",
    body: JSON.stringify(data),
  })
}

/** Poll do estado de um job. `signal` permite abortar o polling no unmount. */
export async function getQueryJob(jobId: string, signal?: AbortSignal) {
  return apiRequest<QueryJobRead>(`${QUERY_JOBS_BASE}/${encodeURIComponent(jobId)}`, { signal })
}

/** Lista jobs recentes org-scoped (limit padrão 50). */
export async function listQueryJobs(limit = 50) {
  const params = new URLSearchParams({ limit: String(limit) })
  return apiRequest<QueryJobRead[]>(`${QUERY_JOBS_BASE}?${params}`)
}

/** Lista detecções org-scoped (filtro opcional por status). */

