import { BASE_URL, apiRequest } from "./_core"
import type {
  AuthStatus,
  AuthUser,
  BootstrapAdminRequest,
  EntraSyncStatus,
  EntraSyncTriggerResult,
  IdentityConfig,
  IdentityConnectionTestResult,
  LoginRequest,
  LoginResponse,
  McpConfig,
  McpStatus,
  UpdateIdentityConfigRequest,
  UpdateMcpConfigRequest,
} from "@/types"

export async function getAuthStatus() {
  return apiRequest<AuthStatus>("/auth/status")
}

/**
 * URL absoluta do início do fluxo SSO (Microsoft Entra). O browser navega
 * diretamente para cá (top-level) — não é um fetch. Usa BASE_URL para
 * funcionar tanto em produção (mesmo domínio) quanto no dev server.
 */
export function ssoLoginUrl(): string {
  return `${BASE_URL}/auth/sso/login`
}

// ── Identity / SSO config (admin) ─────────────────────────────────────
export async function getIdentityConfig() {
  return apiRequest<IdentityConfig>("/identity/config")
}

export async function updateIdentityConfig(data: UpdateIdentityConfigRequest) {
  return apiRequest<IdentityConfig>("/identity/config", {
    method: "PUT",
    body: JSON.stringify(data),
  })
}

export async function testIdentityConnection() {
  return apiRequest<IdentityConnectionTestResult>("/identity/config/test", {
    method: "POST",
  })
}

// ── Servidor MCP embutido (/api/mcp) ──────────────────────────────────
// Admin de plataforma: liga/desliga o endpoint e escolhe o modo de resposta.
export async function getMcpConfig() {
  return apiRequest<McpConfig>("/mcp/config")
}

export async function updateMcpConfig(data: UpdateMcpConfigRequest) {
  return apiRequest<McpConfig>("/mcp/config", {
    method: "PUT",
    body: JSON.stringify(data),
  })
}

// Qualquer usuário autenticado: o que ELE precisa para configurar um cliente.
export async function getMcpStatus() {
  return apiRequest<McpStatus>("/mcp/status")
}

// disparar sync manual de usuários do Entra via Graph
export async function syncEntraNow() {
  return apiRequest<EntraSyncTriggerResult>("/identity/config/sync", {
    method: "POST",
  })
}

// status do último sync de usuários do Entra
export async function getEntraSyncStatus() {
  return apiRequest<EntraSyncStatus>("/identity/config/sync-status")
}

export async function bootstrapAdmin(data: BootstrapAdminRequest) {
  return apiRequest<LoginResponse>("/auth/bootstrap", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function login(data: LoginRequest) {
  return apiRequest<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

export async function logout() {
  return apiRequest<{ detail: string }>("/auth/logout", {
    method: "POST",
  })
}

export async function getCurrentUser() {
  return apiRequest<AuthUser>("/auth/me")
}

/** Persist the UI language on the user's profile so it follows them across
 *  devices. Best-effort — callers ignore failures (e.g. 401
 *  when called from the pre-login language switcher). */
export async function updateMyLocale(locale: string): Promise<AuthUser> {
  return apiRequest<AuthUser>("/auth/me/locale", {
    method: "PUT",
    body: JSON.stringify({ locale }),
  })
}

export async function verifyAdminAccess() {
  return apiRequest<{ allowed: boolean }>("/auth/admin-access")
}

