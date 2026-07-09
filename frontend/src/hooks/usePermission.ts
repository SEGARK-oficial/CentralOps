/**
 * usePermission
 * Verifica se o usuário autenticado possui uma permissão específica.
 *
 * Sprint 4: sem fallback por role. Confia que backend sempre retorna
 * permissions[] em /api/auth/me. Se array ausente ou vazio, retorna false.
 */

import { useAuth } from "@/contexts/AuthContext"

export function usePermission(perm: string): boolean {
  const { user } = useAuth()

  if (!user) return false

  return user.permissions.includes(perm)
}
