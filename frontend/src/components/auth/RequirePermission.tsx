import type React from "react"
import { Navigate } from "react-router-dom"
import { usePermission } from "@/hooks/usePermission"

interface RequirePermissionProps {
  perm: string
  children: React.ReactElement
  fallback?: React.ReactElement
}

/**
 * Guarda de permissão baseado no array de permissões do usuário.
 * Substitui o RoleGuard para rotas que exigem uma permissão específica.
 *
 * Se o usuário não tiver a permissão, renderiza `fallback` ou redireciona
 * para "/" por padrão.
 */
export function RequirePermission({ perm, children, fallback }: RequirePermissionProps): React.ReactElement {
  const can = usePermission(perm)

  if (!can) {
    return fallback ?? <Navigate to="/" replace />
  }

  return children
}
