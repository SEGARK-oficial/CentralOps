import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import type { AppUser, Organization, UpdateUserRequest } from "@/types"

const selectCls =
  "h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20 disabled:cursor-not-allowed disabled:opacity-50"

interface EditUserModalProps {
  open: boolean
  user: AppUser | null
  onClose: () => void
  onSave: (userId: string, payload: UpdateUserRequest) => Promise<void>
  organizations: Organization[]
}

interface FormValues {
  display_name: string
  password: string
  confirm_password: string
  organization_id: number | null
}

export const EditUserModal: React.FC<EditUserModalProps> = ({
  open,
  user,
  onClose,
  onSave,
  organizations,
}) => {
  const { t } = useTranslation("admin")
  const [values, setValues] = useState<FormValues>({ display_name: "", password: "", confirm_password: "", organization_id: null })
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (user) {
      setValues({
        display_name: user.display_name || "",
        password: "",
        confirm_password: "",
        organization_id: user.organization_id ?? null,
      })
      setError(null)
    }
  }, [user])

  const handleClose = () => {
    if (!loading) {
      setError(null)
      onClose()
    }
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!user) return

    if (values.password && values.password.length < 10) {
      setError(t("editUserModal.errors.passwordTooShort"))
      return
    }
    if (values.password && values.password !== values.confirm_password) {
      setError(t("editUserModal.errors.passwordMismatch"))
      return
    }
    if (user.role !== "admin" && values.organization_id === null) {
      setError(t("editUserModal.errors.organizationRequired"))
      return
    }

    setLoading(true)
    setError(null)
    try {
      const payload: UpdateUserRequest = {
        display_name: values.display_name.trim() || undefined,
        organization_id: values.organization_id,
      }
      if (values.password) {
        payload.password = values.password
      }
      await onSave(user.id, payload)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : t("editUserModal.errors.saveFailed"))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal open={open} onClose={handleClose} title={t("editUserModal.title")} size="sm">
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        {error && (
          <Notice variant="danger" title={t("editUserModal.errorTitle")}>
            {error}
          </Notice>
        )}

        <Input
          name="display_name"
          label={t("editUserModal.displayNameLabel")}
          value={values.display_name}
          onChange={(e) => setValues((v) => ({ ...v, display_name: e.target.value }))}
          disabled={loading}
        />

        <div className="space-y-1.5">
          <label htmlFor="edit-user-org" className="text-sm font-medium text-text">
            {t("editUserModal.organizationLabel")}
            {user?.role !== "admin" && <span className="text-danger-500"> *</span>}
          </label>
          <select
            id="edit-user-org"
            data-testid="edit-user-org"
            className={selectCls}
            value={values.organization_id ?? ""}
            onChange={(e) =>
              setValues((v) => ({
                ...v,
                organization_id: e.target.value === "" ? null : Number(e.target.value),
              }))
            }
            disabled={loading}
          >
            <option value="">{t("editUserModal.organizationNone")}</option>
            {organizations.map((org) => (
              <option key={org.id} value={org.id} disabled={!org.is_active}>
                {org.name}{org.is_active ? "" : t("editUserModal.organizationInactiveSuffix")}
              </option>
            ))}
          </select>
        </div>

        <Input
          name="password"
          type="password"
          label={t("editUserModal.newPasswordLabel")}
          helperText={t("editUserModal.newPasswordHelp")}
          value={values.password}
          onChange={(e) => setValues((v) => ({ ...v, password: e.target.value }))}
          disabled={loading}
        />

        <Input
          name="confirm_password"
          type="password"
          label={t("editUserModal.confirmNewPasswordLabel")}
          value={values.confirm_password}
          onChange={(e) => setValues((v) => ({ ...v, confirm_password: e.target.value }))}
          disabled={loading}
        />

        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={loading}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" loading={loading}>
            {t("editUserModal.submit")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
