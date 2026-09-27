import type React from "react"
import { useTranslation } from "react-i18next"
import { ShieldCheckIcon, ShieldHalfIcon, EyeIcon, WrenchIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { usePermission } from "@/hooks/usePermission"
import { formatRelativeDate } from "@/lib/utils"
import type { AppUser, TableColumn, UserRole } from "@/types"

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

// R4-8.6: migrado do `<table>` escrito à mão pro `DataTable` — a coluna de
// Ações continua condicional a `canManage` (o `DataTable` aceita array de
// colunas dinâmico, sem exigir todas fixas). Nenhuma coluna é `sortable`
// (evita trocar o cabeçalho por um botão com ícone ↕, sem ganho aqui).
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

  const columns: TableColumn<AppUser>[] = [
    {
      key: "user",
      title: t("usersTable.columns.user"),
      dataIndex: "display_name",
      render: (_value, u) => {
        const isSelf = currentUserId === u.id
        return (
          <div className="max-w-[220px] space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate font-semibold text-text" title={u.display_name || u.username}>
                {u.display_name || u.username}
              </span>
              {isSelf && <Badge variant="outline" size="sm">{t("usersTable.you")}</Badge>}
            </div>
            <div className="truncate text-xs text-text-tertiary" title={`@${u.username}`}>@{u.username}</div>
          </div>
        )
      },
    },
    {
      key: "role",
      title: t("usersTable.columns.role"),
      dataIndex: "role",
      className: "whitespace-nowrap",
      render: (_value, u) => (
        <Badge variant={ROLE_VARIANT[u.role]} size="sm" className="gap-1.5">
          {ROLE_ICON[u.role]}
          {ROLE_LABEL[u.role]}
        </Badge>
      ),
    },
    {
      key: "organization",
      title: t("usersTable.columns.organization"),
      dataIndex: "organization_name",
      render: (_value, u) => (
        <span className="block max-w-[200px] truncate text-text" title={u.organization_name || "—"}>
          {u.organization_name || "—"}
        </span>
      ),
    },
    {
      key: "status",
      title: t("usersTable.columns.status"),
      dataIndex: "is_active",
      className: "whitespace-nowrap",
      render: (_value, u) => (
        <Badge variant={u.is_active ? "success" : "warning"} size="sm">
          {u.is_active ? t("usersTable.statusActive") : t("usersTable.statusInactive")}
        </Badge>
      ),
    },
    {
      key: "lastAccess",
      title: t("usersTable.columns.lastAccess"),
      dataIndex: "last_login_at",
      className: "whitespace-nowrap text-text-secondary",
      render: (_value, u) => (u.last_login_at ? formatRelativeDate(u.last_login_at) : t("usersTable.never")),
    },
    ...(canManage
      ? [
          {
            key: "actions",
            title: t("usersTable.columns.actions"),
            dataIndex: "id",
            render: (_value, u) => {
              const isSelf = currentUserId === u.id
              const rowBusy = busyUserId === u.id
              return (
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
              )
            },
          } satisfies TableColumn<AppUser>,
        ]
      : []),
  ]

  return (
    <DataTable
      data-testid="users-table"
      data={users}
      columns={columns}
      rowKey="id"
      tableAriaLabel={t("usersTable.ariaLabel")}
      tableClassName="min-w-[860px]"
    />
  )
}
