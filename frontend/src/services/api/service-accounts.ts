import { ADMIN_REDIRECT_PATH, apiRequest } from "./_core"
import type {
  ApiToken,
  ApiTokenCreateRequest,
  ApiTokenCreateResponse,
  ServiceAccount,
  ServiceAccountCreateRequest,
  ServiceAccountUpdateRequest,
} from "@/types"

// ── Service Accounts (admin only) ────────────────────────────────────

const SA_BASE = "/v1/service-accounts"

export async function listServiceAccounts(
  options?: { include_inactive?: boolean },
): Promise<ServiceAccount[]> {
  const qs = options?.include_inactive ? "?include_inactive=true" : ""
  return apiRequest<ServiceAccount[]>(`${SA_BASE}${qs}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function getServiceAccount(saId: number): Promise<ServiceAccount> {
  return apiRequest<ServiceAccount>(`${SA_BASE}/${saId}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function createServiceAccount(
  payload: ServiceAccountCreateRequest,
): Promise<ServiceAccount> {
  return apiRequest<ServiceAccount>(SA_BASE, {
    method: "POST",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function updateServiceAccount(
  saId: number,
  payload: ServiceAccountUpdateRequest,
): Promise<ServiceAccount> {
  return apiRequest<ServiceAccount>(`${SA_BASE}/${saId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function deleteServiceAccount(saId: number): Promise<void> {
  return apiRequest<void>(`${SA_BASE}/${saId}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function listServiceAccountTokens(
  saId: number,
  options?: { include_revoked?: boolean },
): Promise<ApiToken[]> {
  const qs = options?.include_revoked ? "?include_revoked=true" : ""
  return apiRequest<ApiToken[]>(`${SA_BASE}/${saId}/tokens${qs}`, {
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function createServiceAccountToken(
  saId: number,
  payload: ApiTokenCreateRequest,
): Promise<ApiTokenCreateResponse> {
  return apiRequest<ApiTokenCreateResponse>(`${SA_BASE}/${saId}/tokens`, {
    method: "POST",
    body: JSON.stringify(payload),
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

export async function revokeServiceAccountToken(
  saId: number,
  tokenId: number,
): Promise<void> {
  return apiRequest<void>(`${SA_BASE}/${saId}/tokens/${tokenId}`, {
    method: "DELETE",
    forbiddenRedirectTo: ADMIN_REDIRECT_PATH,
  })
}

