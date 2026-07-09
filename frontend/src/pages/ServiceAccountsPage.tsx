"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  AlertTriangleIcon,
  BotIcon,
  CheckCircleIcon,
  CopyIcon,
  KeyIcon,
  PencilIcon,
  PlusIcon,
  RefreshCcwIcon,
  TrashIcon,
  XIcon,
} from "lucide-react"

import * as api from "@/services/api"
import type {
  ApiToken,
  ScopeName,
  ServiceAccount,
  ServiceAccountCreateRequest,
  ServiceAccountUpdateRequest,
} from "@/types"

import { ScopeSelector } from "@/components/tokens/ScopeSelector"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { formatDateTime } from "@/lib/intl"

// ── Helpers reusados de TokensPage ──────────────────────────────────

type ExpiryPreset = "30d" | "60d" | "90d" | "1y" | "never"

function expiryDays(preset: ExpiryPreset): number | null {
  switch (preset) {
    case "30d":
      return 30
    case "60d":
      return 60
    case "90d":
      return 90
    case "1y":
      return 365
    case "never":
      return null
  }
}

function expiryFromPreset(preset: ExpiryPreset): string | null {
  const days = expiryDays(preset)
  if (days === null) return null
  const d = new Date()
  d.setDate(d.getDate() + days)
  return d.toISOString()
}

const ROLE_OPTIONS: ServiceAccount["role"][] = [
  "viewer",
  "operator",
  "engineer",
  "admin",
]

function roleBadgeVariant(
  role: ServiceAccount["role"],
): "outline" | "primary" | "warning" | "danger" {
  switch (role) {
    case "viewer":
      return "outline"
    case "operator":
      return "primary"
    case "engineer":
      return "warning"
    case "admin":
      return "danger"
  }
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? "—" : formatDateTime(d)
}

function isTokenActive(token: ApiToken): boolean {
  if (token.revoked_at) return false
  if (token.expires_at && new Date(token.expires_at) <= new Date()) return false
  return true
}

// ── Página ──────────────────────────────────────────────────────────

export const ServiceAccountsPage: React.FC = () => {
  const { t } = useTranslation("admin")

  const [accounts, setAccounts] = useState<ServiceAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<{
    type: "success" | "error"
    message: string
  } | null>(null)

  const [createOpen, setCreateOpen] = useState(false)
  const [editing, setEditing] = useState<ServiceAccount | null>(null)
  const [tokensModalSa, setTokensModalSa] = useState<ServiceAccount | null>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<ServiceAccount | null>(
    null,
  )
  const [busyId, setBusyId] = useState<number | null>(null)

  const refetch = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.listServiceAccounts({ include_inactive: true })
      setAccounts(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : t("serviceAccounts.feedback.listFailed"))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void refetch()
  }, [refetch])

  // Auto-dismiss do feedback de sucesso (~5s); erros persistem até o usuário agir.
  useEffect(() => {
    if (feedback?.type !== "success") return
    const dismissTimer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(dismissTimer)
  }, [feedback])

  const stats = useMemo(() => {
    const active = accounts.filter((a) => a.is_active).length
    const totalTokens = accounts.reduce((acc, a) => acc + a.active_token_count, 0)
    return { active, total: accounts.length, totalTokens }
  }, [accounts])

  const handleDelete = async () => {
    if (!deleteCandidate) return
    const target = deleteCandidate
    setBusyId(target.id)
    setFeedback(null)
    try {
      await api.deleteServiceAccount(target.id)
      setFeedback({
        type: "success",
        message: t("serviceAccounts.feedback.deleted", { name: target.name }),
      })
      await refetch()
    } catch (e) {
      setFeedback({
        type: "error",
        message: e instanceof Error ? e.message : t("serviceAccounts.feedback.deleteFailed"),
      })
    } finally {
      setDeleteCandidate(null)
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-6" data-testid="service-accounts-page">
      <PageHeader
        icon={<BotIcon size={24} />}
        eyebrow={t("serviceAccounts.eyebrow")}
        title={t("serviceAccounts.title")}
        description={t("serviceAccounts.description")}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              onClick={refetch}
              leftIcon={<RefreshCcwIcon size={16} />}
              disabled={loading}
            >
              {t("common:actions.refresh")}
            </Button>
            <Button
              onClick={() => setCreateOpen(true)}
              leftIcon={<PlusIcon size={16} />}
            >
              {t("serviceAccounts.newServiceAccount")}
            </Button>
          </div>
        }
      />

      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          action={
            <button
              type="button"
              onClick={() => setFeedback(null)}
              aria-label={t("serviceAccounts.closeAriaLabel")}
              className="rounded p-0.5 opacity-70 transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <XIcon size={16} />
            </button>
          }
        >
          {feedback.message}
        </Notice>
      )}

      {error && <Notice variant="danger">{error}</Notice>}

      <Card>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-sm font-semibold">
            {t("serviceAccounts.listTitle")}
            <span className="ml-2 text-text-secondary text-xs">
              {t("serviceAccounts.listSummary", { active: stats.active, total: stats.total, tokens: stats.totalTokens })}
            </span>
          </h2>
        </div>

        {loading ? (
          <div className="flex justify-center py-12">
            <LoadingSpinner />
          </div>
        ) : accounts.length === 0 ? (
          <EmptyState
            icon={<BotIcon size={32} />}
            title={t("serviceAccounts.empty.title")}
            description={t("serviceAccounts.empty.description")}
            action={
              <Button onClick={() => setCreateOpen(true)} leftIcon={<PlusIcon size={16} />}>
                {t("serviceAccounts.newServiceAccount")}
              </Button>
            }
          />
        ) : (
          <>
            {/* Tablet / desktop: tabela com rolagem horizontal segura. */}
            <div className="hidden overflow-x-auto md:block">
              <table
                className="w-full min-w-[760px] text-sm"
                role="table"
                aria-label={t("serviceAccounts.table.ariaLabel")}
              >
                <thead className="bg-bg-subtle text-text-secondary">
                  <tr>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      {t("serviceAccounts.table.columns.name")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.table.columns.role")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.table.columns.status")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-right font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.table.columns.activeTokens")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.table.columns.created")}
                    </th>
                    <th scope="col" className="px-4 py-2">
                      <span className="sr-only">{t("serviceAccounts.table.columns.actions")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map((sa) => (
                    <tr
                      key={sa.id}
                      className="border-t hover:bg-bg-subtle/50"
                      data-testid={`sa-row-${sa.id}`}
                    >
                      <td className="px-4 py-2">
                        <div
                          className="max-w-[260px] truncate font-medium"
                          title={sa.name}
                        >
                          {sa.name}
                        </div>
                        {sa.description && (
                          <div
                            className="max-w-[260px] truncate text-xs text-text-secondary"
                            title={sa.description}
                          >
                            {sa.description}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        <Badge variant={roleBadgeVariant(sa.role)}>{sa.role}</Badge>
                      </td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        {sa.is_active ? (
                          <Badge variant="success">{t("serviceAccounts.table.statusActive")}</Badge>
                        ) : (
                          <Badge variant="danger">{t("serviceAccounts.table.statusInactive")}</Badge>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums whitespace-nowrap">
                        {sa.active_token_count}
                      </td>
                      <td className="px-4 py-2 text-text-secondary whitespace-nowrap">
                        {formatDate(sa.created_at)}
                      </td>
                      <td className="px-4 py-2 text-right space-x-1 whitespace-nowrap">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setTokensModalSa(sa)}
                          leftIcon={<KeyIcon size={14} />}
                        >
                          {t("serviceAccounts.table.tokensAction")}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setEditing(sa)}
                          leftIcon={<PencilIcon size={14} />}
                        >
                          {t("serviceAccounts.table.editAction")}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => setDeleteCandidate(sa)}
                          disabled={busyId === sa.id}
                          leftIcon={<TrashIcon size={14} />}
                        >
                          {t("serviceAccounts.table.deleteAction")}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile: cada Service Account vira um cartão — sem scroll horizontal. */}
            <div className="space-y-3 p-4 md:hidden">
              {accounts.map((sa) => (
                <div
                  key={sa.id}
                  className="rounded-xl border border-border bg-surface p-4"
                  data-testid={`sa-card-${sa.id}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div
                        className="truncate font-medium"
                        title={sa.name}
                      >
                        {sa.name}
                      </div>
                      {sa.description && (
                        <div
                          className="truncate text-xs text-text-secondary"
                          title={sa.description}
                        >
                          {sa.description}
                        </div>
                      )}
                    </div>
                    {sa.is_active ? (
                      <Badge variant="success">{t("serviceAccounts.table.statusActive")}</Badge>
                    ) : (
                      <Badge variant="danger">{t("serviceAccounts.table.statusInactive")}</Badge>
                    )}
                  </div>

                  <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
                    <div>
                      <dt className="text-text-secondary">{t("serviceAccounts.table.columns.role")}</dt>
                      <dd className="mt-0.5">
                        <Badge variant={roleBadgeVariant(sa.role)} size="sm">
                          {sa.role}
                        </Badge>
                      </dd>
                    </div>
                    <div>
                      <dt className="text-text-secondary">{t("serviceAccounts.table.columns.activeTokens")}</dt>
                      <dd className="mt-0.5 tabular-nums">{sa.active_token_count}</dd>
                    </div>
                    <div className="col-span-2">
                      <dt className="text-text-secondary">{t("serviceAccounts.table.columns.created")}</dt>
                      <dd className="mt-0.5 text-text-secondary">
                        {formatDate(sa.created_at)}
                      </dd>
                    </div>
                  </dl>

                  <div className="mt-3 flex flex-wrap justify-end gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setTokensModalSa(sa)}
                      leftIcon={<KeyIcon size={14} />}
                    >
                      {t("serviceAccounts.table.tokensAction")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setEditing(sa)}
                      leftIcon={<PencilIcon size={14} />}
                    >
                      {t("serviceAccounts.table.editAction")}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setDeleteCandidate(sa)}
                      disabled={busyId === sa.id}
                      leftIcon={<TrashIcon size={14} />}
                    >
                      {t("serviceAccounts.table.deleteAction")}
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      <CreateServiceAccountModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={async (sa) => {
          setFeedback({
            type: "success",
            message: t("serviceAccounts.feedback.created", { name: sa.name }),
          })
          await refetch()
        }}
      />

      <EditServiceAccountModal
        sa={editing}
        onClose={() => setEditing(null)}
        onUpdated={async (sa) => {
          setFeedback({
            type: "success",
            message: t("serviceAccounts.feedback.updated", { name: sa.name }),
          })
          await refetch()
        }}
      />

      <ServiceAccountTokensModal
        sa={tokensModalSa}
        onClose={() => setTokensModalSa(null)}
        onChanged={async () => {
          await refetch()
        }}
      />

      <ConfirmDialog
        open={!!deleteCandidate}
        title={t("serviceAccounts.deleteDialog.title")}
        description={
          deleteCandidate
            ? t("serviceAccounts.deleteDialog.description", { name: deleteCandidate.name, count: deleteCandidate.active_token_count })
            : ""
        }
        confirmLabel={t("serviceAccounts.table.deleteAction")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        loading={busyId === deleteCandidate?.id}
        onConfirm={handleDelete}
        onClose={() => setDeleteCandidate(null)}
      />
    </div>
  )
}

// ── Modal: criar SA ─────────────────────────────────────────────────

interface CreateSaModalProps {
  open: boolean
  onClose: () => void
  onCreated: (sa: ServiceAccount) => Promise<void> | void
}

const CreateServiceAccountModal: React.FC<CreateSaModalProps> = ({
  open,
  onClose,
  onCreated,
}) => {
  const { t } = useTranslation("admin")
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")
  const [role, setRole] = useState<ServiceAccount["role"]>("viewer")
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!open) {
      const resetTimer = setTimeout(() => {
        setName("")
        setDescription("")
        setRole("viewer")
        setError(null)
      }, 200)
      return () => clearTimeout(resetTimer)
    }
  }, [open])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      setError(t("serviceAccounts.createModal.errors.nameRequired"))
      return
    }
    setCreating(true)
    setError(null)
    const payload: ServiceAccountCreateRequest = {
      name: name.trim(),
      description: description.trim() || null,
      role,
    }
    try {
      const sa = await api.createServiceAccount(payload)
      await onCreated(sa)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : t("serviceAccounts.createModal.errors.createFailed"))
    } finally {
      setCreating(false)
    }
  }

  const ROLE_LABELS_LOCAL: Record<ServiceAccount["role"], string> = {
    viewer: t("serviceAccounts.roleLabels.viewer"),
    operator: t("serviceAccounts.roleLabels.operator"),
    engineer: t("serviceAccounts.roleLabels.engineer"),
    admin: t("serviceAccounts.roleLabels.admin"),
  }

  return (
    <Modal open={open} onClose={onClose} title={t("serviceAccounts.createModal.title")} size="md">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="create-sa-name" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
            {t("serviceAccounts.createModal.nameLabel")}
          </label>
          <Input
            id="create-sa-name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("serviceAccounts.createModal.namePlaceholder")}
            maxLength={100}
            autoFocus
            required
            autoComplete="off"
            data-1p-ignore="true"
            data-lpignore="true"
            data-form-type="other"
          />
          <p className="mt-1 text-xs text-text-secondary">
            <Trans i18nKey="serviceAccounts.createModal.nameHelp" t={t} components={{ code: <code /> }} />
          </p>
        </div>

        <div>
          <label htmlFor="create-sa-desc" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
            {t("serviceAccounts.createModal.descriptionLabel")}
          </label>
          <Input
            id="create-sa-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("serviceAccounts.createModal.descriptionPlaceholder")}
            maxLength={300}
          />
        </div>

        <div>
          <label htmlFor="create-sa-role" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
            {t("serviceAccounts.createModal.roleLabel")}
          </label>
          <select
            id="create-sa-role"
            value={role}
            onChange={(e) => setRole(e.target.value as ServiceAccount["role"])}
            className="mt-1 block w-full rounded-md border border-border bg-bg px-3 py-2 text-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
          >
            {ROLE_OPTIONS.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABELS_LOCAL[r]}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-text-secondary">
            <Trans i18nKey="serviceAccounts.createModal.roleHelp" t={t} components={{ code: <code /> }} />
          </p>
        </div>

        {error && <Notice variant="danger">{error}</Notice>}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" disabled={creating}>
            {creating ? t("serviceAccounts.createModal.submitting") : t("serviceAccounts.createModal.submit")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

// ── Modal: editar SA ────────────────────────────────────────────────

interface EditSaModalProps {
  sa: ServiceAccount | null
  onClose: () => void
  onUpdated: (sa: ServiceAccount) => Promise<void> | void
}

const EditServiceAccountModal: React.FC<EditSaModalProps> = ({
  sa,
  onClose,
  onUpdated,
}) => {
  const { t } = useTranslation("admin")
  const ROLE_LABELS_LOCAL: Record<ServiceAccount["role"], string> = {
    viewer: t("serviceAccounts.roleLabels.viewer"),
    operator: t("serviceAccounts.roleLabels.operator"),
    engineer: t("serviceAccounts.roleLabels.engineer"),
    admin: t("serviceAccounts.roleLabels.admin"),
  }
  const [description, setDescription] = useState("")
  const [role, setRole] = useState<ServiceAccount["role"]>("viewer")
  const [isActive, setIsActive] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (sa) {
      setDescription(sa.description ?? "")
      setRole(sa.role)
      setIsActive(sa.is_active)
      setError(null)
    }
  }, [sa])

  if (!sa) return null

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setError(null)
    const payload: ServiceAccountUpdateRequest = {
      description: description.trim() || null,
      role,
      is_active: isActive,
    }
    try {
      const updated = await api.updateServiceAccount(sa.id, payload)
      await onUpdated(updated)
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : t("serviceAccounts.editModal.errors.updateFailed"))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      open={!!sa}
      onClose={onClose}
      title={t("serviceAccounts.editModal.title", { name: sa.name })}
      size="md"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label htmlFor="edit-sa-desc" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
            {t("serviceAccounts.editModal.descriptionLabel")}
          </label>
          <Input
            id="edit-sa-desc"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            maxLength={300}
          />
        </div>

        <div>
          <label htmlFor="edit-sa-role" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
            {t("serviceAccounts.editModal.roleLabel")}
          </label>
          <select
            id="edit-sa-role"
            value={role}
            onChange={(e) => setRole(e.target.value as ServiceAccount["role"])}
            className="mt-1 block w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
          >
            {ROLE_OPTIONS.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABELS_LOCAL[r]}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-text-secondary">
            {t("serviceAccounts.editModal.roleHelp")}
          </p>
        </div>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={isActive}
            onChange={(e) => setIsActive(e.target.checked)}
          />
          <span>{t("serviceAccounts.editModal.activeLabel")}</span>
        </label>
        {!isActive && (
          <Notice variant="warning">
            <div className="flex items-start gap-2">
              <AlertTriangleIcon size={16} className="mt-0.5 shrink-0" />
              <div className="text-sm">
                {t("serviceAccounts.editModal.deactivateWarning")}
              </div>
            </div>
          </Notice>
        )}

        {error && <Notice variant="danger">{error}</Notice>}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" type="button" onClick={onClose}>
            {t("common:actions.cancel")}
          </Button>
          <Button type="submit" disabled={saving}>
            {saving ? t("serviceAccounts.editModal.submitting") : t("serviceAccounts.editModal.submit")}
          </Button>
        </div>
      </form>
    </Modal>
  )
}

// ── Modal: tokens do SA ─────────────────────────────────────────────

interface SaTokensModalProps {
  sa: ServiceAccount | null
  onClose: () => void
  onChanged: () => Promise<void> | void
}

const ServiceAccountTokensModal: React.FC<SaTokensModalProps> = ({
  sa,
  onClose,
  onChanged,
}) => {
  const { t } = useTranslation("admin")
  const [tokens, setTokens] = useState<ApiToken[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [revokeCandidate, setRevokeCandidate] = useState<ApiToken | null>(null)
  const [busy, setBusy] = useState(false)

  const refetch = useCallback(async () => {
    if (!sa) return
    setLoading(true)
    setError(null)
    try {
      const data = await api.listServiceAccountTokens(sa.id, {
        include_revoked: true,
      })
      setTokens(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : t("serviceAccounts.tokensModal.errors.listFailed"))
    } finally {
      setLoading(false)
    }
  }, [sa, t])

  useEffect(() => {
    if (sa) void refetch()
    else setTokens([])
  }, [sa, refetch])

  const handleRevoke = async () => {
    if (!sa || !revokeCandidate) return
    setBusy(true)
    try {
      await api.revokeServiceAccountToken(sa.id, revokeCandidate.id)
      setRevokeCandidate(null)
      await refetch()
      await onChanged()
    } catch (e) {
      setError(e instanceof Error ? e.message : t("serviceAccounts.tokensModal.errors.revokeFailed"))
    } finally {
      setBusy(false)
    }
  }

  if (!sa) return null

  return (
    <>
      <Modal
        open={!!sa && !createOpen}
        onClose={onClose}
        title={t("serviceAccounts.tokensModal.title", { name: sa.name })}
        size="lg"
      >
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm text-text-secondary">
              <Trans
                i18nKey="serviceAccounts.tokensModal.boundTokens"
                t={t}
                values={{ name: sa.name, role: sa.role }}
                components={{ strong: <strong /> }}
              />{" "}
              <Badge variant={roleBadgeVariant(sa.role)} size="sm">
                {sa.role}
              </Badge>
            </p>
            <Button
              size="sm"
              onClick={() => setCreateOpen(true)}
              leftIcon={<PlusIcon size={14} />}
            >
              {t("serviceAccounts.tokensModal.newToken")}
            </Button>
          </div>

          {error && <Notice variant="danger">{error}</Notice>}

          {loading ? (
            <div className="flex justify-center py-6">
              <LoadingSpinner />
            </div>
          ) : tokens.length === 0 ? (
            <EmptyState
              icon={<KeyIcon size={48} />}
              title={t("serviceAccounts.tokensModal.empty.title")}
              description={t("serviceAccounts.tokensModal.empty.description")}
            />
          ) : (
            <div className="overflow-x-auto">
              <table
                className="w-full min-w-[560px] text-sm"
                role="table"
                aria-label={t("serviceAccounts.tokensModal.ariaLabel", { name: sa.name })}
              >
                <thead className="bg-bg-subtle text-text-secondary">
                  <tr>
                    <th scope="col" className="px-3 py-2 text-left font-medium">
                      {t("serviceAccounts.tokensModal.columns.name")}
                    </th>
                    <th
                      scope="col"
                      className="px-3 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.tokensModal.columns.prefix")}
                    </th>
                    <th
                      scope="col"
                      className="px-3 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.tokensModal.columns.status")}
                    </th>
                    <th
                      scope="col"
                      className="px-3 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("serviceAccounts.tokensModal.columns.expires")}
                    </th>
                    <th scope="col" className="px-3 py-2">
                      <span className="sr-only">{t("serviceAccounts.tokensModal.columns.actions")}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tokens.map((tok) => (
                    <tr key={tok.id} className="border-t">
                      <td className="px-3 py-2">
                        <div className="max-w-[220px] truncate" title={tok.name}>
                          {tok.name}
                        </div>
                      </td>
                      <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">
                        {tok.token_prefix}…
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        {isTokenActive(tok) ? (
                          <Badge variant="success">{t("serviceAccounts.tokensModal.statusActive")}</Badge>
                        ) : tok.revoked_at ? (
                          <Badge variant="danger">{t("serviceAccounts.tokensModal.statusRevoked")}</Badge>
                        ) : (
                          <Badge variant="warning">{t("serviceAccounts.tokensModal.statusExpired")}</Badge>
                        )}
                      </td>
                      <td className="px-3 py-2 text-text-secondary whitespace-nowrap">
                        {formatDate(tok.expires_at)}
                      </td>
                      <td className="px-3 py-2 text-right whitespace-nowrap">
                        {isTokenActive(tok) && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setRevokeCandidate(tok)}
                            leftIcon={<TrashIcon size={14} />}
                          >
                            {t("serviceAccounts.tokensModal.revokeAction")}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Modal>

      <CreateSaTokenModal
        sa={sa}
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={async () => {
          await refetch()
          await onChanged()
        }}
      />

      <ConfirmDialog
        open={!!revokeCandidate}
        title={t("serviceAccounts.tokensModal.revokeDialog.title")}
        description={
          revokeCandidate
            ? t("serviceAccounts.tokensModal.revokeDialog.description", { name: revokeCandidate.name })
            : ""
        }
        confirmLabel={t("serviceAccounts.tokensModal.revokeAction")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        loading={busy}
        onConfirm={handleRevoke}
        onClose={() => setRevokeCandidate(null)}
      />
    </>
  )
}

// ── Modal: emitir token pra SA ──────────────────────────────────────

interface CreateSaTokenModalProps {
  sa: ServiceAccount
  open: boolean
  onClose: () => void
  onCreated: () => Promise<void> | void
}

const CreateSaTokenModal: React.FC<CreateSaTokenModalProps> = ({
  sa,
  open,
  onClose,
  onCreated,
}) => {
  const { t } = useTranslation("admin")

  const EXPIRY_OPTIONS: { value: ExpiryPreset; label: string }[] = [
    { value: "30d", label: t("expiryOptions.30d") },
    { value: "60d", label: t("expiryOptions.60d") },
    { value: "90d", label: t("expiryOptions.90d") },
    { value: "1y", label: t("expiryOptions.1y") },
    { value: "never", label: t("expiryOptions.never") },
  ]

  const [name, setName] = useState("")
  const [preset, setPreset] = useState<ExpiryPreset>("90d")
  const [scopes, setScopes] = useState<ScopeName[] | null>(null)
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [createdRaw, setCreatedRaw] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!open) {
      const resetTimer = setTimeout(() => {
        setName("")
        setPreset("90d")
        setScopes(null)
        setError(null)
        setCreatedRaw(null)
        setCopied(false)
      }, 200)
      return () => clearTimeout(resetTimer)
    }
  }, [open])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      setError(t("serviceAccounts.createTokenModal.errors.nameRequired"))
      return
    }
    setCreating(true)
    setError(null)
    try {
      const expiresAt = expiryFromPreset(preset)
      const result = await api.createServiceAccountToken(sa.id, {
        name: name.trim(),
        expires_at: expiresAt,
        is_eternal: expiresAt === null,
        scopes: scopes && scopes.length > 0 ? scopes : null,
      })
      setCreatedRaw(result.token)
    } catch (err) {
      setError(err instanceof Error ? err.message : t("serviceAccounts.createTokenModal.errors.createFailed"))
    } finally {
      setCreating(false)
    }
  }

  const handleCopy = async () => {
    if (!createdRaw) return
    try {
      await navigator.clipboard.writeText(createdRaw)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      /* fallback: select manual */
    }
  }

  const handleClose = async () => {
    if (createdRaw) {
      await onCreated()
    }
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={createdRaw ? t("serviceAccounts.createTokenModal.titleCreated") : t("serviceAccounts.createTokenModal.titleNew", { name: sa.name })}
      size="md"
      closeOnOverlayClick={!createdRaw}
      closeOnEscape={!createdRaw}
    >
      {createdRaw ? (
        <div className="space-y-4">
          <Notice variant="warning">
            <div className="font-semibold mb-1">{t("serviceAccounts.createTokenModal.copyNow")}</div>
            <div className="text-sm">
              {t("serviceAccounts.createTokenModal.copyNowDescription")}
            </div>
          </Notice>

          <div>
            <label className="text-xs font-medium uppercase tracking-wider text-text-secondary">
              {t("serviceAccounts.createTokenModal.tokenLabel")}
            </label>
            <div className="mt-1 flex items-center gap-2 rounded border bg-bg-subtle p-3 font-mono text-sm break-all">
              <span className="flex-1" data-testid="sa-created-raw-token">
                {createdRaw}
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={handleCopy}
                leftIcon={
                  copied ? <CheckCircleIcon size={14} /> : <CopyIcon size={14} />
                }
              >
                {copied ? t("serviceAccounts.createTokenModal.copied") : t("serviceAccounts.createTokenModal.copy")}
              </Button>
            </div>
          </div>

          <div className="flex justify-end">
            <Button onClick={handleClose}>{t("common:actions.close")}</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label htmlFor="sa-token-name" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
              {t("serviceAccounts.createTokenModal.nameLabel")}
            </label>
            <Input
              id="sa-token-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("serviceAccounts.createTokenModal.namePlaceholder", { name: sa.name })}
              maxLength={100}
              autoFocus
              required
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-form-type="other"
            />
          </div>

          <div>
            <label htmlFor="sa-token-expiry" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
              {t("serviceAccounts.createTokenModal.expiryLabel")}
            </label>
            <select
              id="sa-token-expiry"
              value={preset}
              onChange={(e) => setPreset(e.target.value as ExpiryPreset)}
              className="mt-1 block w-full rounded-md border border-border bg-bg px-3 py-2 text-sm"
            >
              {EXPIRY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label id="sa-token-scopes-label" className="text-xs font-medium uppercase tracking-wider text-text-secondary">
              {t("serviceAccounts.createTokenModal.scopesLabel")}
            </label>
            <div className="mt-1" role="group" aria-labelledby="sa-token-scopes-label">
              <ScopeSelector
                value={scopes}
                onChange={setScopes}
                disabled={creating}
              />
            </div>
            <p className="mt-1 text-xs text-text-secondary">
              <Trans i18nKey="serviceAccounts.createTokenModal.effectiveLimit" t={t} values={{ role: sa.role }} components={{ code: <code /> }} />
            </p>
          </div>

          {error && <Notice variant="danger">{error}</Notice>}

          <div className="flex justify-end gap-2">
            <Button variant="ghost" type="button" onClick={onClose}>
              {t("common:actions.cancel")}
            </Button>
            <Button type="submit" disabled={creating}>
              {creating ? t("serviceAccounts.createTokenModal.submitting") : t("serviceAccounts.createTokenModal.submit")}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  )
}

export default ServiceAccountsPage
