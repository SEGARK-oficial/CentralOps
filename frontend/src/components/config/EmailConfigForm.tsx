"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { MailIcon, PlusIcon, SendIcon, ShieldIcon, TrashIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { useForm } from "@/hooks/useForm"
import { Button } from "@/components/ui/Button/Button"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { Input } from "@/components/ui/Input/Input"
import { Notice } from "@/components/ui/Notice/Notice"
import type { EmailConfig, EmailRecipient, UpdateEmailConfigRequest } from "@/types"

interface Props {
  config: EmailConfig | null
  recipients: EmailRecipient[]
  loading?: boolean
  saving?: boolean
  testing?: boolean
  addingRecipient?: boolean
  removingRecipientId?: number | null
  feedback?: { type: "success" | "error"; message: string } | null
  onSave: (data: UpdateEmailConfigRequest) => Promise<boolean>
  onAdd: (email: string) => Promise<boolean>
  onDelete: (id: number) => Promise<boolean>
  onTest: () => Promise<boolean>
}

interface EmailConfigFormValues {
  smtp_host: string
  smtp_port: number
  smtp_user: string
  smtp_password: string
  sender: string
  use_tls: boolean
}

const defaultFormValues: EmailConfigFormValues = {
  smtp_host: "",
  smtp_port: 25,
  smtp_user: "",
  smtp_password: "",
  sender: "",
  use_tls: false,
}

export const EmailConfigForm: React.FC<Props> = ({
  config,
  recipients,
  loading = false,
  saving = false,
  testing = false,
  addingRecipient = false,
  removingRecipientId = null,
  feedback = null,
  onSave,
  onAdd,
  onDelete,
  onTest,
}) => {
  const { t } = useTranslation("config")
  const [clearStoredPassword, setClearStoredPassword] = useState(false)
  const [recipientEmail, setRecipientEmail] = useState("")
  const [recipientToDelete, setRecipientToDelete] = useState<EmailRecipient | null>(null)

  const initialValues: EmailConfigFormValues = config
    ? {
        smtp_host: config.smtp_host || "",
        smtp_port: config.smtp_port || 25,
        smtp_user: config.smtp_user || "",
        smtp_password: "",
        sender: config.sender || "",
        use_tls: !!config.use_tls,
      }
    : defaultFormValues

  const { values, handleChange, handleSubmit, isSubmitting, setFieldValue } = useForm<EmailConfigFormValues>({
    initialValues,
    onSubmit: async (formValues) => {
      const saved = await onSave({
        smtp_host: formValues.smtp_host,
        smtp_port: Number(formValues.smtp_port),
        smtp_user: formValues.smtp_user,
        ...(formValues.smtp_password ? { smtp_password: formValues.smtp_password } : {}),
        ...(clearStoredPassword ? { clear_smtp_password: true } : {}),
        sender: formValues.sender,
        use_tls: formValues.use_tls,
      })

      if (saved) {
        setFieldValue("smtp_password", "")
        setClearStoredPassword(false)
      }
    },
  })

  useEffect(() => {
    if (!config) return

    setFieldValue("smtp_host", config.smtp_host || "")
    setFieldValue("smtp_port", config.smtp_port || 25)
    setFieldValue("smtp_user", config.smtp_user || "")
    setFieldValue("smtp_password", "")
    setFieldValue("sender", config.sender || "")
    setFieldValue("use_tls", !!config.use_tls)
    setClearStoredPassword(false)
  }, [config, setFieldValue])

  const formDisabled = loading || saving || isSubmitting

  const handleAddRecipient = async (event: React.FormEvent) => {
    event.preventDefault()
    const email = recipientEmail.trim()
    if (!email) return

    const added = await onAdd(email)
    if (added) {
      setRecipientEmail("")
    }
  }

  const handleConfirmDelete = async () => {
    if (!recipientToDelete) return
    const deleted = await onDelete(recipientToDelete.id)
    if (deleted) {
      setRecipientToDelete(null)
    }
  }

  return (
    <div className="space-y-6">
      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          title={feedback.type === "success" ? t("email.resultTitleSuccess") : t("email.resultTitleError")}
        >
          {feedback.message}
        </Notice>
      )}

      <form onSubmit={handleSubmit} className="space-y-6" noValidate>
        <div className="grid gap-4 md:grid-cols-2">
          <Input
            name="smtp_host"
            label={t("email.fields.smtpHost")}
            value={values.smtp_host || ""}
            onChange={handleChange}
            required
            disabled={formDisabled}
          />

          <Input
            name="smtp_port"
            type="number"
            label={t("email.fields.port")}
            value={values.smtp_port?.toString() || "25"}
            onChange={handleChange}
            required
            disabled={formDisabled}
          />

          <Input
            name="smtp_user"
            label={t("email.fields.user")}
            value={values.smtp_user || ""}
            onChange={handleChange}
            helperText={t("email.fields.userHelper")}
            disabled={formDisabled}
          />

          <Input
            name="sender"
            label={t("email.fields.sender")}
            placeholder={t("email.fields.senderPlaceholder")}
            value={values.sender || ""}
            onChange={handleChange}
            required
            disabled={formDisabled}
          />

          <div className="md:col-span-2">
            <Input
              name="smtp_password"
              type="password"
              label={t("email.fields.password")}
              value={values.smtp_password || ""}
              onChange={(event) => {
                handleChange(event)
                if (event.target.value) {
                  setClearStoredPassword(false)
                }
              }}
              helperText={
                config?.smtp_password_configured
                  ? t("email.fields.passwordHelperConfigured")
                  : t("email.fields.passwordHelperEmpty")
              }
              disabled={formDisabled}
            />
          </div>
        </div>

        <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface-tertiary/40 p-4">
          <Checkbox
            name="use_tls"
            checked={values.use_tls}
            onChange={handleChange}
            disabled={formDisabled}
            label={t("email.useTls")}
            description={t("email.useTlsDescription")}
          />

          {config?.smtp_password_configured && (
            <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
              <BadgeLike>
                <ShieldIcon size={14} />
                {t("email.passwordStoredSecurely")}
              </BadgeLike>
              <Button
                type="button"
                size="sm"
                variant={clearStoredPassword ? "danger" : "outline"}
                onClick={() => setClearStoredPassword((current) => !current)}
                disabled={formDisabled}
              >
                {clearStoredPassword ? t("email.keepCurrentPassword") : t("email.removeStoredPassword")}
              </Button>
            </div>
          )}
        </div>

        {clearStoredPassword && (
          <Notice variant="warning" title={t("email.passwordMarkedForRemovalTitle")}>
            {t("email.passwordMarkedForRemovalBody")}
          </Notice>
        )}

        <div className="flex flex-wrap justify-end gap-3">
          <Button type="button" variant="outline" onClick={() => void onTest()} loading={testing} disabled={formDisabled} leftIcon={<MailIcon size={16} />}>
            {t("email.sendTest")}
          </Button>
          <Button type="submit" loading={saving || isSubmitting} leftIcon={<SendIcon size={16} />}>
            {t("email.saveConfig")}
          </Button>
        </div>
      </form>

      <div className="space-y-4 rounded-2xl border border-border bg-surface-secondary/40 p-5">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-base font-semibold text-text">{t("email.recipients.title")}</h3>
            <p className="text-sm text-text-secondary">{t("email.recipients.description")}</p>
          </div>
          <span className="rounded-full bg-surface px-3 py-1 text-xs font-semibold text-text-secondary ring-1 ring-border">
            {t("email.recipients.count", { count: recipients.length })}
          </span>
        </div>

        <form onSubmit={handleAddRecipient} className="flex flex-col gap-3 sm:flex-row">
          <div className="flex-1">
            <Input
              type="email"
              label={t("email.recipients.newLabel")}
              placeholder={t("email.recipients.newPlaceholder")}
              value={recipientEmail}
              onChange={(event) => setRecipientEmail(event.target.value)}
              disabled={loading || addingRecipient}
              required
            />
          </div>
          <div className="sm:pt-7">
            <Button type="submit" leftIcon={<PlusIcon size={14} />} loading={addingRecipient} disabled={loading}>
              {t("email.recipients.add")}
            </Button>
          </div>
        </form>

        {recipients.length === 0 ? (
          <div className="rounded-xl border border-dashed border-border bg-surface px-4 py-6">
            <EmptyState
              icon={<MailIcon size={42} />}
              title={t("email.recipients.emptyTitle")}
              description={t("email.recipients.emptyDescription")}
              className="py-2"
            />
          </div>
        ) : (
          <div className="space-y-3">
            {recipients.map((recipient) => (
              <div
                key={recipient.id}
                className="flex flex-col gap-3 rounded-xl border border-border bg-surface px-4 py-3 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex items-center gap-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary-50 text-primary-700">
                    <MailIcon size={16} />
                  </div>
                  <div>
                    <div className="font-medium text-text">{recipient.email}</div>
                    <div className="text-xs text-text-tertiary">{t("email.recipients.idLabel", { id: recipient.id })}</div>
                  </div>
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  leftIcon={<TrashIcon size={14} />}
                  onClick={() => setRecipientToDelete(recipient)}
                  loading={removingRecipientId === recipient.id}
                  disabled={loading || (removingRecipientId !== null && removingRecipientId !== recipient.id)}
                  aria-label={t("email.recipients.removeAriaLabel", { email: recipient.email })}
                >
                  {t("email.recipients.remove")}
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={recipientToDelete !== null}
        title={t("email.recipients.deleteTitle")}
        description={
          <>
            {t("email.recipients.deleteDescriptionPrefix")}{" "}
            <strong className="text-text">{recipientToDelete?.email}</strong>
            {t("email.recipients.deleteDescriptionSuffix")}
          </>
        }
        confirmLabel={t("email.recipients.remove")}
        confirmVariant="danger"
        loading={recipientToDelete !== null && removingRecipientId === recipientToDelete.id}
        onConfirm={handleConfirmDelete}
        onClose={() => setRecipientToDelete(null)}
        data-testid="email-recipient-delete-dialog"
      />
    </div>
  )
}

const BadgeLike: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <span className="inline-flex items-center gap-1.5 rounded-full bg-success-50 px-3 py-1 text-xs font-semibold text-success-700">
    {children}
  </span>
)
