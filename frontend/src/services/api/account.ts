import { apiRequest } from "./_core"
import type {
  AccountProfile,
  PasswordChangeRequest,
  PasswordChangeResult,
  RevokeOtherSessionsResult,
  SelfProfileUpdate,
} from "@/types"

// ── Self-service account (própria conta) ──────────────────────────────

/** Perfil completo do próprio usuário (com created_at/last_login_at) para a
 *  página de conta. Só lê a identidade do caller — sem escopo de org. */
export async function getMyProfile(): Promise<AccountProfile> {
  return apiRequest<AccountProfile>("/auth/me/profile")
}

/** Atualiza os campos que o usuário pode alterar em si mesmo (display_name/
 *  email/locale). Trocar o e-mail exige `current_password` (reautenticação). */
export async function updateMyProfile(data: SelfProfileUpdate): Promise<AccountProfile> {
  return apiRequest<AccountProfile>("/auth/me", {
    method: "PATCH",
    body: JSON.stringify(data),
  })
}

/** Troca a própria senha (contas locais). Revoga as demais sessões e mantém a
 *  atual; devolve quantas foram encerradas. */
export async function changeMyPassword(
  data: PasswordChangeRequest,
): Promise<PasswordChangeResult> {
  return apiRequest<PasswordChangeResult>("/auth/me/password", {
    method: "POST",
    body: JSON.stringify(data),
  })
}

/** Encerra todas as OUTRAS sessões do usuário, mantendo a atual. */
export async function revokeMyOtherSessions(): Promise<RevokeOtherSessionsResult> {
  return apiRequest<RevokeOtherSessionsResult>("/auth/me/sessions/revoke-others", {
    method: "POST",
  })
}

