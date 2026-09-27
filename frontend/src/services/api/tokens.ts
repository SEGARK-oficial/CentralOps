import { apiRequest } from "./_core"
import type {
  ApiToken,
  ApiTokenCreateRequest,
  ApiTokenCreateResponse,
  ScopeName,
} from "@/types"

// ── Personal Access Tokens + Service Accounts ──────


export async function listApiTokens(
  options?: { include_revoked?: boolean },
): Promise<ApiToken[]> {
  const qs = options?.include_revoked ? "?include_revoked=true" : ""
  return apiRequest<ApiToken[]>(`/v1/tokens${qs}`)
}

export async function createApiToken(
  payload: ApiTokenCreateRequest,
): Promise<ApiTokenCreateResponse> {
  return apiRequest<ApiTokenCreateResponse>("/v1/tokens", {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function revokeApiToken(tokenId: number): Promise<void> {
  return apiRequest<void>(`/v1/tokens/${encodeURIComponent(tokenId)}`, {
    method: "DELETE",
  })
}

/** Lista de scopes válidos (= Permission enum no backend). Cacheável.  */
export async function listScopes(): Promise<ScopeName[]> {
  return apiRequest<ScopeName[]>("/v1/tokens/scopes")
}

