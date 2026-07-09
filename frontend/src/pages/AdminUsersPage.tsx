"use client"

import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import { RefreshCcwIcon, SearchIcon, ShieldIcon, UserPlusIcon, UsersIcon } from "lucide-react"
import { EditUserModal } from "@/components/admin/EditUserModal"
import { EditUserRoleModal } from "@/components/admin/EditUserRoleModal"
import { NewUserModal } from "@/components/admin/NewUserModal"
import { RolePermissionsViewer } from "@/components/admin/RolePermissionsViewer"
import { UsersTable } from "@/components/admin/UsersTable"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { useAuth } from "@/contexts/AuthContext"
import { useUsers } from "@/hooks/useUsers"
import { usePermission } from "@/hooks/usePermission"
import * as api from "@/services/api"
import type { AppUser, Organization, UpdateUserRequest, UserRole } from "@/types"

export const AdminUsersPage: React.FC = () => {
  const { t } = useTranslation("admin")
  const { user: currentUser, refreshSession } = useAuth()
  const canManage = usePermission("user.manage")

  const { users, isLoading, error, refetch, createUser, updateUser, deleteUser } = useUsers()

  const [feedback, setFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null)
  const [busyUserId, setBusyUserId] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  // Organizações p/ atribuir ao usuário (criação/edição). Inclui inativas
  // (renderizadas como disabled) para refletir o vínculo atual sem permitir
  // mover para uma org desativada.
  const [organizations, setOrganizations] = useState<Organization[]>([])

  useEffect(() => {
    let cancelled = false
    void api
      .listOrganizations(true)
      .then((orgs) => {
        if (!cancelled) setOrganizations(orgs)
      })
      .catch(() => {
        // Falha ao listar orgs não bloqueia a tela; o seletor fica vazio
        // (admin global ainda criável). Erros de usuários têm seu próprio aviso.
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Auto-dismiss apenas para sucesso (~5s); erros persistem até nova ação.
  useEffect(() => {
    if (feedback?.type !== "success") return
    const timer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(timer)
  }, [feedback])

  // Modais
  const [newUserOpen, setNewUserOpen] = useState(false)
  const [editRoleTarget, setEditRoleTarget] = useState<AppUser | null>(null)
  const [editUserTarget, setEditUserTarget] = useState<AppUser | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<AppUser | null>(null)
  const [matrixOpen, setMatrixOpen] = useState(false)

  const showSuccess = (msg: string) => setFeedback({ type: "success", message: msg })
  const showError = (msg: string) => setFeedback({ type: "error", message: msg })

  const handleToggleActive = async (target: AppUser) => {
    setBusyUserId(target.id)
    setFeedback(null)
    try {
      await updateUser(target.id, { is_active: !target.is_active })
      if (currentUser?.id === target.id) await refreshSession()
      showSuccess(target.is_active ? t("users.feedback.userDeactivated") : t("users.feedback.userReactivated"))
    } catch (e) {
      showError(e instanceof Error ? e.message : t("users.feedback.toggleActiveFailed"))
    } finally {
      setBusyUserId(null)
    }
  }

  const handleDelete = async () => {
    if (!deleteCandidate) return
    const target = deleteCandidate
    setBusyUserId(target.id)
    setFeedback(null)
    try {
      await deleteUser(target.id)
      showSuccess(t("users.feedback.userDeleted"))
    } catch (e) {
      showError(e instanceof Error ? e.message : t("users.feedback.deleteFailed"))
    } finally {
      setDeleteCandidate(null)
      setBusyUserId(null)
    }
  }

  const handleSaveRole = async (userId: string, role: UserRole, _reason?: string) => {
    await updateUser(userId, { role })
    if (currentUser?.id === userId) await refreshSession()
    showSuccess(t("users.feedback.roleUpdated"))
  }

  const handleSaveUser = async (userId: string, payload: UpdateUserRequest) => {
    await updateUser(userId, payload)
    if (currentUser?.id === userId) await refreshSession()
    showSuccess(t("users.feedback.dataUpdated"))
  }

  const totalAdmins = users.filter((u) => u.role === "admin").length
  const totalActive = users.filter((u) => u.is_active).length

  // Filtro client-side por nome de exibição ou usuário (login).
  const query = search.trim().toLowerCase()
  const filteredUsers = useMemo(() => {
    if (!query) return users
    return users.filter(
      (u) =>
        (u.display_name ?? "").toLowerCase().includes(query) ||
        u.username.toLowerCase().includes(query),
    )
  }, [users, query])

  return (
    <div className="space-y-6" data-testid="admin-users-page">
      <PageHeader
        icon={<UsersIcon size={24} />}
        eyebrow={t("users.eyebrow")}
        title={t("users.title")}
        description={t("users.description")}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              onClick={() => setMatrixOpen(true)}
              leftIcon={<ShieldIcon size={16} />}
            >
              {t("users.viewPermissions")}
            </Button>
            <Button
              variant="outline"
              onClick={() => refetch()}
              leftIcon={<RefreshCcwIcon size={16} />}
              disabled={isLoading}
            >
              {t("common:actions.refresh")}
            </Button>
            {canManage && (
              <Button
                onClick={() => setNewUserOpen(true)}
                leftIcon={<UserPlusIcon size={16} />}
                data-testid="new-user-button"
              >
                {t("users.newUser")}
              </Button>
            )}
          </div>
        }
      />

      <div className="grid gap-4 sm:grid-cols-3">
        <Card padding="sm" className="shadow-sm">
          <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("users.stats.total")}</div>
          <div className="mt-2 flex items-end gap-2">
            <span className="text-2xl font-bold text-text">{users.length}</span>
            <Badge variant="outline" size="sm">{t("users.stats.totalUnit")}</Badge>
          </div>
        </Card>
        <Card padding="sm" className="shadow-sm">
          <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("users.stats.active")}</div>
          <div className="mt-2 flex items-end gap-2">
            <span className="text-2xl font-bold text-text">{totalActive}</span>
            <Badge variant="success" size="sm">{t("users.stats.activeUnit")}</Badge>
          </div>
        </Card>
        <Card padding="sm" className="shadow-sm">
          <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("users.stats.admins")}</div>
          <div className="mt-2 flex items-end gap-2">
            <span className="text-2xl font-bold text-text">{totalAdmins}</span>
            <Badge variant="primary" size="sm">{t("users.stats.adminsUnit")}</Badge>
          </div>
        </Card>
      </div>

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          title={feedback.type === "success" ? t("users.feedback.operationDone") : t("users.feedback.error")}
          action={
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setFeedback(null)}
              aria-label={t("users.feedback.closeAriaLabel")}
            >
              {t("common:actions.close")}
            </Button>
          }
        >
          {feedback.message}
        </Notice>
      )}

      {error && (
        <Notice variant="danger" title={t("users.feedback.loadFailedTitle")}>
          {error.message}
        </Notice>
      )}

      {isLoading ? (
        <LoadingSpinner size="lg" text={t("users.loading")} className="py-20" />
      ) : users.length === 0 ? (
        <EmptyState
          icon={<UsersIcon size={48} />}
          title={t("users.empty.title")}
          description={t("users.empty.description")}
          action={
            canManage ? (
              <Button
                onClick={() => setNewUserOpen(true)}
                leftIcon={<UserPlusIcon size={16} />}
                data-testid="empty-new-user-button"
              >
                {t("users.newUser")}
              </Button>
            ) : undefined
          }
        />
      ) : (
        <div className="space-y-4">
          <div className="max-w-sm">
            <Input
              type="search"
              leftIcon={<SearchIcon size={16} />}
              placeholder={t("users.search.placeholder")}
              aria-label={t("users.search.ariaLabel")}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              data-testid="users-search"
            />
          </div>

          {filteredUsers.length === 0 ? (
            <EmptyState
              icon={<SearchIcon size={48} />}
              title={t("users.empty.searchTitle")}
              description={t("users.empty.searchDescription", { query: search.trim() })}
              action={
                <Button variant="outline" onClick={() => setSearch("")}>
                  {t("users.empty.clearSearch")}
                </Button>
              }
            />
          ) : (
            <UsersTable
              users={filteredUsers}
              currentUserId={currentUser?.id ?? null}
              busyUserId={busyUserId}
              onEditRole={setEditRoleTarget}
              onEditUser={setEditUserTarget}
              onToggleActive={(u) => void handleToggleActive(u)}
              onDelete={setDeleteCandidate}
            />
          )}
        </div>
      )}

      {/* Modais */}
      <NewUserModal
        open={newUserOpen}
        organizations={organizations}
        onClose={() => setNewUserOpen(false)}
        onCreate={async (payload) => {
          await createUser(payload)
          showSuccess(t("users.feedback.userCreated"))
        }}
      />

      <EditUserRoleModal
        open={!!editRoleTarget}
        user={editRoleTarget}
        onClose={() => setEditRoleTarget(null)}
        onSave={handleSaveRole}
      />

      <EditUserModal
        open={!!editUserTarget}
        user={editUserTarget}
        organizations={organizations}
        onClose={() => setEditUserTarget(null)}
        onSave={handleSaveUser}
      />

      <ConfirmDialog
        open={!!deleteCandidate}
        title={t("users.deleteDialog.title")}
        description={
          <p>
            <Trans
              i18nKey="users.deleteDialog.description"
              t={t}
              values={{ name: deleteCandidate?.display_name || deleteCandidate?.username }}
              components={{ strong: <strong /> }}
            />
          </p>
        }
        confirmLabel={t("users.deleteDialog.confirmLabel")}
        loading={busyUserId === deleteCandidate?.id}
        onConfirm={handleDelete}
        onClose={() => setDeleteCandidate(null)}
      />

      <RolePermissionsViewer
        open={matrixOpen}
        onClose={() => setMatrixOpen(false)}
      />
    </div>
  )
}

export default AdminUsersPage
