import type React from "react"
import { useEffect, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import type { AppUser, UserRole } from "@/types"

interface EditUserRoleModalProps {
  open: boolean
  user: AppUser | null
  onClose: () => void
  onSave: (userId: string, role: UserRole, reason?: string) => Promise<void>
}

export const EditUserRoleModal: React.FC<EditUserRoleModalProps> = ({
  open,
  user,
  onClose,
  onSave,
}) => {
  const { t } = useTranslation("admin")

  const ROLE_OPTIONS: { value: UserRole; label: string; description: string }[] = [
    { value: "viewer", label: t("editUserRoleModal.roleOptions.viewer.label"), description: t("editUserRoleModal.roleOptions.viewer.description") },
    { value: "operator", label: t("editUserRoleModal.roleOptions.operator.label"), description: t("editUserRoleModal.roleOptions.operator.description") },
    { value: "engineer", label: t("editUserRoleModal.roleOptions.engineer.label"), description: t("editUserRoleModal.roleOptions.engineer.description") },
    { value: "admin", label: t("editUserRoleModal.roleOptions.admin.label"), description: t("editUserRoleModal.roleOptions.admin.description") },
  ]

  const [selectedRole, setSelectedRole] = useState<UserRole>("viewer")
  const [reason, setReason] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Sincroniza o role selecionado com o usuário recebido ao abrir
  useEffect(() => {
    if (open && user) {
      setSelectedRole(user.role)
      setReason("")
      setError(null)
    }
  }, [open, user])

  const handleClose = () => {
    if (!loading) {
      setError(null)
      onClose()
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!user) return
    setLoading(true)
    setError(null)
    try {
      await onSave(user.id, selectedRole, reason.trim() || undefined)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : t("editUserRoleModal.errors.changeFailed"))
    } finally {
      setLoading(false)
    }
  }

  const roleDescription = ROLE_OPTIONS.find((o) => o.value === selectedRole)?.description

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={t("editUserRoleModal.title")}
      size="sm"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <Notice variant="danger" title={t("editUserRoleModal.cannotChangeTitle")}>
            {error}
          </Notice>
        )}

        {user && (
          <p className="text-sm text-text-secondary">
            <Trans
              i18nKey="editUserRoleModal.changingRoleFor"
              t={t}
              values={{ name: user.display_name || user.username }}
              components={{ strong: <strong /> }}
            />
          </p>
        )}

        <div className="space-y-1.5">
          <label htmlFor="role-select" className="text-sm font-medium text-text">
            {t("editUserRoleModal.roleLabel")}
          </label>
          <select
            id="role-select"
            data-testid="role-select"
            className="h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            value={selectedRole}
            onChange={(e) => setSelectedRole(e.target.value as UserRole)}
            disabled={loading}
          >
            {ROLE_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          {roleDescription && (
            <p className="text-xs text-text-tertiary">{roleDescription}</p>
          )}
        </div>

        <div className="space-y-1.5">
          <label htmlFor="role-change-reason" className="text-sm font-medium text-text">
            {t("editUserRoleModal.reasonLabel")} <span className="text-text-tertiary text-xs">{t("editUserRoleModal.reasonOptional")}</span>
          </label>
          <textarea
            id="role-change-reason"
            className="w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-text transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20 disabled:cursor-not-allowed disabled:opacity-50 resize-none"
            rows={3}
            placeholder={t("editUserRoleModal.reasonPlaceholder")}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={loading}
          />
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={loading}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" loading={loading} disabled={!user || user.role === selectedRole}>
            {t("editUserRoleModal.submit")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
