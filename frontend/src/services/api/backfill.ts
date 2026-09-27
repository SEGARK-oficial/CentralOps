import { apiRequest } from "./_core"
import type { ApiRequestOptions } from "./_core"
import type {
  BackfillJob,
  BackfillJobStatus,
  CreateBackfillJobRequest,
} from "@/types"

// ── Sprint 2: Backfill API ─────────────────────────────────────────────


export async function listBackfillJobs(
  integrationId: number,
  filters?: { limit?: number; offset?: number; status?: BackfillJobStatus },
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<{ items: BackfillJob[]; total: number; limit: number; offset: number }> {
  const sp = new URLSearchParams()
  if (filters?.limit != null) sp.set("limit", String(filters.limit))
  if (filters?.offset != null) sp.set("offset", String(filters.offset))
  if (filters?.status) sp.set("status", filters.status)
  const qs = sp.toString()
  return apiRequest<{ items: BackfillJob[]; total: number; limit: number; offset: number }>(
    `/integrations/${encodeURIComponent(integrationId)}/backfill-jobs${qs ? `?${qs}` : ""}`,
    options,
  )
}

export async function getBackfillJob(
  jobId: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BackfillJob> {
  return apiRequest<BackfillJob>(`/backfill-jobs/${encodeURIComponent(jobId)}`, options)
}

export async function createBackfillJob(
  integrationId: number,
  payload: CreateBackfillJobRequest,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BackfillJob> {
  return apiRequest<BackfillJob>(`/integrations/${encodeURIComponent(integrationId)}/backfill`, {
    method: "POST",
    body: JSON.stringify(payload),
    ...options,
  })
}

export async function cancelBackfillJob(
  jobId: string,
  options?: Pick<ApiRequestOptions, "signal">,
): Promise<BackfillJob> {
  return apiRequest<BackfillJob>(`/backfill-jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
    ...options,
  })
}

