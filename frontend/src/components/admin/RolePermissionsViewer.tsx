import type React from "react"
import { useTranslation } from "react-i18next"
import { CheckIcon, XIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { usePermissionsMatrix } from "@/hooks/usePermissionsMatrix"
import type { UserRole } from "@/types"

const ROLE_ORDER: UserRole[] = ["viewer", "operator", "engineer", "admin"]

const ROLE_VARIANT: Record<UserRole, "outline" | "default" | "warning" | "primary"> = {
  viewer: "outline",
  operator: "default",
  engineer: "warning",
  admin: "primary",
}

interface RolePermissionsViewerProps {
  open: boolean
  onClose: () => void
}

export const RolePermissionsViewer: React.FC<RolePermissionsViewerProps> = ({ open, onClose }) => {
  const { t } = useTranslation("admin")
  const { matrix, isLoading, error } = usePermissionsMatrix()

  // Coletamos todos os perms únicos da matriz
  const allPerms = matrix
    ? Array.from(new Set(Object.values(matrix).flat())).sort()
    : []

  return (
    <Modal open={open} onClose={onClose} title={t("rolePermissionsViewer.title")} size="lg">
      {isLoading && <LoadingSpinner size="md" text={t("rolePermissionsViewer.loading")} className="py-8" />}

      {error && (
        <Notice variant="danger" title={t("rolePermissionsViewer.loadFailedTitle")}>
          {error.message}
        </Notice>
      )}

      {matrix && !isLoading && (
        <div className="space-y-4">
          <p className="text-sm text-text-secondary">
            {t("rolePermissionsViewer.description")}
          </p>

          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="min-w-full text-xs" role="table" aria-label={t("rolePermissionsViewer.tableAriaLabel")}>
              <thead>
                <tr className="border-b border-border bg-surface-tertiary">
                  <th scope="col" className="px-4 py-2 text-left font-semibold text-text-secondary">{t("rolePermissionsViewer.permissionColumn")}</th>
                  {ROLE_ORDER.map((role) => (
                    <th key={role} scope="col" className="px-4 py-2 text-center font-semibold text-text-secondary">
                      <Badge variant={ROLE_VARIANT[role]} size="sm">{role}</Badge>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {allPerms.map((perm) => (
                  <tr key={perm} className="hover:bg-surface-tertiary/40">
                    <td className="px-4 py-2 font-mono text-text">{perm}</td>
                    {ROLE_ORDER.map((role) => {
                      const has = matrix[role]?.includes(perm)
                      return (
                        <td key={role} className="px-4 py-2 text-center">
                          {has ? (
                            <CheckIcon size={14} className="mx-auto text-success-600" aria-label={t("rolePermissionsViewer.hasPermissionAriaLabel", { role, perm })} />
                          ) : (
                            <XIcon size={14} className="mx-auto text-text-tertiary" aria-label={t("rolePermissionsViewer.noPermissionAriaLabel", { role, perm })} />
                          )}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </Modal>
  )
}
