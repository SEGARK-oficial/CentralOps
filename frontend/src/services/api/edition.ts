import { ApiRequestError, BASE_URL, apiRequest } from "./_core"
import type {
  EditionStatus,
  LicenseStatus,
} from "@/types"

// ── Edição / licença (open-core) ─────────────────────────────────────

export async function getEdition() {
  return apiRequest<EditionStatus>("/edition")
}

// Ativação de licença: persiste o token assinado CIFRADO no banco, lido
// DB-first pelo resolver de edição. Admin-only no backend.
export async function getLicenseStatus() {
  return apiRequest<LicenseStatus>("/licenses/status")
}

export async function activateLicense(token: string) {
  return apiRequest<LicenseStatus>("/licenses/activate", {
    method: "POST",
    body: JSON.stringify({ token }),
  })
}

export async function deactivateLicense() {
  return apiRequest<LicenseStatus>("/licenses", { method: "DELETE" })
}

/** Total de organizações ATIVAS (lê o header X-Total-Count, que ``apiRequest``
 *  descarta). Usado para a UX de teto de tier (Starter single-tenant). */
export async function countActiveOrganizations(): Promise<number> {
  const res = await fetch(`${BASE_URL}/organizations/?status=active&size=1`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
  })
  if (!res.ok) {
    // Espelha apiRequest: 401 dispara o fluxo de sessão expirada (logout/redirect).
    if (res.status === 401 && typeof window !== "undefined") {
      window.dispatchEvent(new CustomEvent("app-auth-expired"))
    }
    throw new ApiRequestError(`HTTP error! status: ${res.status}`, res.status)
  }
  // Guard de NaN: header malformado não deve vazar "NaN / N" para o badge.
  const total = res.headers.get("X-Total-Count")
  const n = total ? Number.parseInt(total, 10) : 0
  return Number.isFinite(n) ? n : 0
}

