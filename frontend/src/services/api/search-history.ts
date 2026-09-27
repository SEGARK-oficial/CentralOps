import i18n from "@/i18n"
import { BASE_URL, apiRequest } from "./_core"
import type {
  AuditFilters,
  AuditHistoryItem,
  HistoryItem,
  SearchHistoryItem,
} from "@/types"

// Sem chamador hoje em `src/` nem `web-ee/` (achado durante a R4-9.1) — a
// forma de busca síncrona por client_id foi substituída pelo fluxo de Query
// Jobs. Mantidas (não removidas: fora do escopo desta rodada de lint) com
// `unknown` em vez de `any`: o resultado de uma busca SQL arbitrária contra
// dados de SIEM não tem forma fixa nenhuma pra tipar melhor, e `unknown`
// obriga quem reativar isto a validar antes de usar.
export async function runSearch(clientId: number, payload: Record<string, unknown>) {
  return apiRequest<unknown>(`/search/${encodeURIComponent(clientId)}`, {
    method: "POST",
    body: JSON.stringify(payload),
  })
}

export async function waitResults(clientId: number, searchId: string) {
  return apiRequest<unknown>(`/search/${encodeURIComponent(clientId)}/${encodeURIComponent(searchId)}/wait`)
}

export async function getSearchStatus(clientId: number, searchId: string) {
  return apiRequest<unknown>(`/search/${encodeURIComponent(clientId)}/${encodeURIComponent(searchId)}/status`)
}

export async function fetchResults(clientId: number, searchId: string) {
  return apiRequest<unknown>(`/search/${encodeURIComponent(clientId)}/${encodeURIComponent(searchId)}`)
}

// History API functions
export async function listHistory() {
  return apiRequest<HistoryItem[]>("/history/")
}

export async function listAuditHistory() {
  return apiRequest<AuditHistoryItem[]>("/history/audit")
}

function buildQueryParams(filters?: AuditFilters) {
  const params = new URLSearchParams()

  if (!filters) return params.toString()

  for (const [key, value] of Object.entries(filters)) {
    if (typeof value === "string" && value.trim()) {
      params.set(key, value.trim())
    }
  }

  return params.toString()
}

export async function listAuditHistoryFiltered(filters?: AuditFilters) {
  const params = buildQueryParams(filters)
  return apiRequest<AuditHistoryItem[]>(`/history/audit${params ? `?${params}` : ""}`)
}

export async function downloadAuditHistoryCSV(filters?: AuditFilters) {
  const params = buildQueryParams(filters)
  const response = await fetch(`${BASE_URL}/history/audit/csv${params ? `?${params}` : ""}`, {
    credentials: "include",
  })
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("app-auth-expired"))
  }
  if (!response.ok) throw new Error(i18n.t("alerts:history.errors.auditExportFailed"))

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = "audit-history.csv"
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

export async function listSearchHistory(clientId?: number) {
  const params = clientId ? `?client_id=${clientId}` : ""
  return apiRequest<SearchHistoryItem[]>(`/search/history${params}`)
}

export async function getStoredResult(searchId: string) {
  return apiRequest<SearchHistoryItem>(`/search/history/result/${encodeURIComponent(searchId)}`)
}

export async function downloadStoredCSV(searchId: string) {
  const response = await fetch(`${BASE_URL}/search/history/result/${encodeURIComponent(searchId)}/csv`, {
    credentials: "include",
  })
  if (response.status === 401 && typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("app-auth-expired"))
  }
  if (!response.ok) {
    // R4-6.2: literal PT fixo — a mesma chave já existe (usada em
    // `useHistory.ts` para o mesmo cenário de download de CSV).
    let errorMessage = i18n.t("alerts:history.errors.downloadCsvFailed")
    try {
      const errorData = await response.json()
      if (typeof errorData?.detail === "string") {
        errorMessage = errorData.detail
      }
    } catch {
      // Ignore JSON parsing errors and keep the fallback message.
    }
    throw new Error(errorMessage)
  }

  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = `${searchId}.csv`
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

// Queries API functions

