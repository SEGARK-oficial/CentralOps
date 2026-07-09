import type React from "react"
import { useTranslation } from "react-i18next"
import { ShieldCheckIcon, ShieldHalfIcon, EyeIcon, WrenchIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { usePermission } from "@/hooks/usePermission"
import { formatRelativeDate } from "@/lib/utils"
import type { AppUser, UserRole } from "@/types"

const ROLE_VARIANT: Record<UserRole, "default" | "primary" | "success" | "warning" | "danger" | "outline"> = {
  viewer: "outline",
  operator: "default",
  engineer: "warning",
  admin: "primary",
}

const ROLE_ICON: Record<UserRole, React.ReactNode> = {
  viewer: <EyeIcon size={12} />,
  operator: <WrenchIcon size={12} />,
  engineer: <ShieldHalfIcon size={12} />,
  admin: <ShieldCheckIcon size={12} />,
}

interface UsersTableProps {
  users: AppUser[]
  currentUserId: string | null
  busyUserId: string | null
  onEditRole: (user: AppUser) => void
  onEditUser: (user: AppUser) => void
  onToggleActive: (user: AppUser) => void
  onDelete: (user: AppUser) => void
}

export const UsersTable: React.FC<UsersTableProps> = ({
  users,
  currentUserId,
  busyUserId,
  onEditRole,
  onEditUser,
  onToggleActive,
  onDelete,
}) => {
  const { t } = useTranslation("admin")
  const canManage = usePermission("user.manage")

  const ROLE_LABEL: Record<UserRole, string> = {
    viewer: t("usersTable.roleLabels.viewer"),
    operator: t("usersTable.roleLabels.operator"),
    engineer: t("usersTable.roleLabels.engineer"),
    admin: t("usersTable.roleLabels.admin"),
  }

  return (
    <div className="overflow-hidden rounded-xl border border-border" data-testid="users-table">
      {/* min-w fixo + overflow-x-auto: a coluna de Ações rola horizontalmente em
          telas estreitas em vez de transbordar/quebrar de forma desalinhada. */}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] text-sm" role="table" aria-label={t("usersTable.ariaLabel")}>
          <thead className="bg-surface-tertiary">
            <tr className="border-b border-border">
              <th scope="col" className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary">{t("usersTable.columns.user")}</th>
              <th scope="col" className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary">{t("usersTable.columns.role")}</th>
              <th scope="col" className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary">{t("usersTable.columns.organization")}</th>
              <th scope="col" className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary">{t("usersTable.columns.status")}</th>
              <th scope="col" className="whitespace-nowrap px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary">{t("usersTable.columns.lastAccess")}</th>
              {canManage && (
                <th scope="col" className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary">{t("usersTable.columns.actions")}</th>
              )}
            </tr>
          </thead>
          <tbody className="divide-y divide-border bg-surface">
            {users.map((u) => {
              const isSelf = currentUserId === u.id
              const rowBusy = busyUserId === u.id

              return (
                <tr key={u.id} className="transition-colors hover:bg-surface-tertiary/40">
                  <td className="px-4 py-4 align-top text-sm">
                    <div className="max-w-[220px] space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-semibold text-text" title={u.display_name || u.username}>{u.display_name || u.username}</span>
                        {isSelf && (
                          <Badge variant="outline" size="sm">{t("usersTable.you")}</Badge>
                        )}
                      </div>
                      <div className="truncate text-xs text-text-tertiary" title={`@${u.username}`}>@{u.username}</div>
                    </div>
                  </td>
                  <td className="px-4 py-4 align-top text-sm">
                    <Badge variant={ROLE_VARIANT[u.role]} size="sm" className="gap-1.5">
                      {ROLE_ICON[u.role]}
                      {ROLE_LABEL[u.role]}
                    </Badge>
                  </td>
                  <td className="px-4 py-4 align-top text-sm">
                    <span className="block max-w-[200px] truncate text-text" title={u.organization_name || "—"}>{u.organization_name || "—"}</span>
                  </td>
                  <td className="px-4 py-4 align-top text-sm">
                    <Badge variant={u.is_active ? "success" : "warning"} size="sm">
                      {u.is_active ? t("usersTable.statusActive") : t("usersTable.statusInactive")}
                    </Badge>
                  </td>
                  <td className="whitespace-nowrap px-4 py-4 align-top text-sm text-text-secondary">
                    {u.last_login_at ? formatRelativeDate(u.last_login_at) : t("usersTable.never")}
                  </td>
                  {canManage && (
                    <td className="px-4 py-4 align-top text-sm">
                      <div className="flex flex-wrap gap-1.5">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => onEditRole(u)}
                          disabled={rowBusy}
                          data-testid={`edit-role-${u.id}`}
                        >
                          {t("usersTable.actions.editRole")}
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => onEditUser(u)}
                          disabled={rowBusy}
                          data-testid={`edit-user-${u.id}`}
                        >
                          {t("usersTable.actions.editData")}
                        </Button>
                        <Button
                          size="sm"
                          variant={u.is_active ? "outline" : "primary"}
                          onClick={() => onToggleActive(u)}
                          disabled={rowBusy || (isSelf && u.is_active)}
                          data-testid={`toggle-active-${u.id}`}
                        >
                          {u.is_active ? t("usersTable.actions.deactivate") : t("usersTable.actions.reactivate")}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={() => onDelete(u)}
                          disabled={rowBusy || isSelf}
                          data-testid={`delete-user-${u.id}`}
                        >
                          {t("usersTable.actions.delete")}
                        </Button>
                      </div>
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
