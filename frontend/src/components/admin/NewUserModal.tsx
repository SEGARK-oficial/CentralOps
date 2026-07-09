import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Input } from "@/components/ui/Input/Input"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import type { CreateUserRequest, Organization, UserRole } from "@/types"

const selectCls =
  "h-9 w-full rounded-md border border-border bg-surface px-3 text-sm text-text transition-colors focus:outline-none focus:border-primary-500 focus:ring-2 focus:ring-primary-500/20 disabled:cursor-not-allowed disabled:opacity-50"

interface NewUserModalProps {
  open: boolean
  onClose: () => void
  onCreate: (payload: CreateUserRequest) => Promise<void>
  organizations: Organization[]
}

interface FormValues {
  username: string
  display_name: string
  password: string
  confirm_password: string
  role: UserRole
  organization_id: number | null
}

const empty: FormValues = {
  username: "",
  display_name: "",
  password: "",
  confirm_password: "",
  role: "viewer",
  organization_id: null,
}

export const NewUserModal: React.FC<NewUserModalProps> = ({ open, onClose, onCreate, organizations }) => {
  const { t } = useTranslation("admin")
  const [values, setValues] = useState<FormValues>(empty)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const ROLE_OPTIONS: { value: UserRole; label: string }[] = [
    { value: "viewer", label: t("newUserModal.roleOptions.viewer") },
    { value: "operator", label: t("newUserModal.roleOptions.operator") },
    { value: "engineer", label: t("newUserModal.roleOptions.engineer") },
    { value: "admin", label: t("newUserModal.roleOptions.admin") },
  ]

  const handleClose = () => {
    if (!loading) {
      setValues(empty)
      setError(null)
      onClose()
    }
  }

  const set = (field: keyof FormValues, value: string) =>
    setValues((v) => ({ ...v, [field]: value }))

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)

    if (!values.username.trim()) {
      setError(t("newUserModal.errors.usernameRequired"))
      return
    }
    if (!values.password) {
      setError(t("newUserModal.errors.passwordRequired"))
      return
    }
    if (values.password.length < 10) {
      setError(t("newUserModal.errors.passwordTooShort"))
      return
    }
    if (values.password !== values.confirm_password) {
      setError(t("newUserModal.errors.passwordMismatch"))
      return
    }
    // Usuário operacional (não-admin) é escopado por org: sem org, ele não
    // enxerga nenhum dado. Admin é global por design — org é opcional.
    if (values.role !== "admin" && values.organization_id === null) {
      setError(t("newUserModal.errors.organizationRequired"))
      return
    }

    setLoading(true)
    try {
      await onCreate({
        username: values.username.trim(),
        password: values.password,
        display_name: values.display_name.trim() || undefined,
        role: values.role,
        organization_id: values.organization_id,
      })
      setValues(empty)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : t("newUserModal.errors.createFailed"))
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal open={open} onClose={handleClose} title={t("newUserModal.title")} size="md">
      <form onSubmit={handleSubmit} className="space-y-4" noValidate>
        {error && (
          <Notice variant="danger" title={t("editUserModal.errorTitle")}>
            {error}
          </Notice>
        )}

        <Input
          name="display_name"
          label={t("newUserModal.displayNameLabel")}
          value={values.display_name}
          onChange={(e) => set("display_name", e.target.value)}
          disabled={loading}
        />

        <Input
          name="username"
          label={t("newUserModal.usernameLabel")}
          value={values.username}
          onChange={(e) => set("username", e.target.value)}
          required
          disabled={loading}
        />

        <div className="space-y-1.5">
          <label htmlFor="new-user-role" className="text-sm font-medium text-text">
            {t("newUserModal.roleLabel")}
          </label>
          <select
            id="new-user-role"
            className={selectCls}
            value={values.role}
            onChange={(e) => set("role", e.target.value)}
            disabled={loading}
          >
            {ROLE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
        </div>

        <div className="space-y-1.5">
          <label htmlFor="new-user-org" className="text-sm font-medium text-text">
            {t("newUserModal.organizationLabel")}
            {values.role !== "admin" && <span className="text-danger-500"> *</span>}
          </label>
          <select
            id="new-user-org"
            data-testid="new-user-org"
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
            <option value="">{t("newUserModal.organizationNone")}</option>
            {organizations.map((org) => (
              <option key={org.id} value={org.id} disabled={!org.is_active}>
                {org.name}{org.is_active ? "" : t("newUserModal.organizationInactiveSuffix")}
              </option>
            ))}
          </select>
          <p className="text-xs text-text-tertiary">
            {t("newUserModal.organizationHelp")}
          </p>
        </div>

        <Input
          name="password"
          type="password"
          label={t("newUserModal.passwordLabel")}
          autoComplete="new-password"
          value={values.password}
          onChange={(e) => set("password", e.target.value)}
          required
          disabled={loading}
        />

        <Input
          name="confirm_password"
          type="password"
          label={t("newUserModal.confirmPasswordLabel")}
          autoComplete="new-password"
          value={values.confirm_password}
          onChange={(e) => set("confirm_password", e.target.value)}
          required
          disabled={loading}
        />

        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={loading}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" loading={loading}>
            {t("newUserModal.submit")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}
