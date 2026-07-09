"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import {
  AlertTriangleIcon,
  CheckCircleIcon,
  CopyIcon,
  KeyIcon,
  PlusIcon,
  RefreshCcwIcon,
  TrashIcon,
} from "lucide-react"

import * as api from "@/services/api"
import type { ApiToken, ScopeName } from "@/types"

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

// ── Helpers ──────────────────────────────────────────────────────────

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

function formatExpiresAt(isoString: string | null, t: TFunction<"admin">): string {
  if (!isoString) return t("tokens.neverExpires")
  const d = new Date(isoString)
  if (Number.isNaN(d.getTime())) return "—"
  return formatDateTime(d)
}

function formatLastUsed(isoString: string | null, t: TFunction<"admin">): string {
  if (!isoString) return t("tokens.neverUsed")
  const d = new Date(isoString)
  if (Number.isNaN(d.getTime())) return "—"
  return formatDateTime(d)
}

function isExpired(token: ApiToken): boolean {
  if (!token.expires_at) return false
  return new Date(token.expires_at) <= new Date()
}

function tokenStatusBadge(token: ApiToken, t: TFunction<"admin">): React.ReactNode {
  if (token.revoked_at) {
    return <Badge variant="danger">{t("tokens.statusRevoked")}</Badge>
  }
  if (isExpired(token)) {
    return <Badge variant="danger">{t("tokens.statusExpired")}</Badge>
  }
  if (!token.expires_at) {
    return <Badge variant="warning">{t("tokens.statusEternal")}</Badge>
  }
  return <Badge variant="success">{t("tokens.statusActive")}</Badge>
}

function feedbackVariant(type: "success" | "error"): "success" | "danger" {
  return type === "success" ? "success" : "danger"
}

// ── Página ──────────────────────────────────────────────────────────

export const TokensPage: React.FC = () => {
  const { t } = useTranslation("admin")
  const [tokens, setTokens] = useState<ApiToken[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [revokeCandidate, setRevokeCandidate] = useState<ApiToken | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [feedback, setFeedback] = useState<{
    type: "success" | "error"
    message: string
  } | null>(null)

  const refetch = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await api.listApiTokens()
      setTokens(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : t("tokens.feedback.listFailed"))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void refetch()
  }, [refetch])

  // Auto-dismiss de feedback de sucesso após ~5s. Erros persistem.
  useEffect(() => {
    if (feedback?.type !== "success") return
    const dismissTimer = setTimeout(() => setFeedback(null), 5000)
    return () => clearTimeout(dismissTimer)
  }, [feedback])

  const activeCount = useMemo(
    () => tokens.filter((tok) => !tok.revoked_at && !isExpired(tok)).length,
    [tokens],
  )

  const handleRevoke = async () => {
    if (!revokeCandidate) return
    const target = revokeCandidate
    setBusyId(target.id)
    setFeedback(null)
    try {
      await api.revokeApiToken(target.id)
      setFeedback({
        type: "success",
        message: t("tokens.feedback.revoked", { name: target.name }),
      })
      await refetch()
    } catch (e) {
      setFeedback({
        type: "error",
        message: e instanceof Error ? e.message : t("tokens.feedback.revokeFailed"),
      })
    } finally {
      setRevokeCandidate(null)
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-6" data-testid="tokens-page">
      <PageHeader
        icon={<KeyIcon size={24} />}
        eyebrow={t("tokens.eyebrow")}
        title={t("tokens.title")}
        description={t("tokens.description")}
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
              {t("tokens.newToken")}
            </Button>
          </div>
        }
      />

      {feedback && (
        <Notice
          variant={feedbackVariant(feedback.type)}
          action={
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setFeedback(null)}
              aria-label={t("tokens.closeAriaLabel")}
            >
              {t("common:actions.close")}
            </Button>
          }
        >
          {feedback.message}
        </Notice>
      )}

      {error && <Notice variant="danger">{error}</Notice>}

      <Card>
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-sm font-semibold">
            {t("tokens.listTitle")}
            <span className="ml-2 text-text-secondary text-xs">
              {t("tokens.listSummary", { active: activeCount, total: tokens.length })}
            </span>
          </h2>
        </div>

        {loading ? (
          <div className="flex justify-center py-12">
            <LoadingSpinner />
          </div>
        ) : tokens.length === 0 ? (
          <EmptyState
            icon={<KeyIcon size={32} />}
            title={t("tokens.empty.title")}
            description={t("tokens.empty.description")}
            action={
              <Button onClick={() => setCreateOpen(true)} leftIcon={<PlusIcon size={16} />}>
                {t("tokens.newToken")}
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
                aria-label={t("tokens.table.ariaLabel")}
              >
                <thead className="bg-bg-subtle text-text-secondary">
                  <tr>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      {t("tokens.table.columns.name")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("tokens.table.columns.prefix")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("tokens.table.columns.status")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-left font-medium whitespace-nowrap"
                    >
                      {t("tokens.table.columns.expires")}
                    </th>
                    <th scope="col" className="px-4 py-2 text-left font-medium">
                      {t("tokens.table.columns.lastUsed")}
                    </th>
                    <th
                      scope="col"
                      className="px-4 py-2 text-right font-medium whitespace-nowrap"
                    >
                      {t("tokens.table.columns.uses")}
                    </th>
                    <th scope="col" className="px-4 py-2">
                      <span className="sr-only">Ações</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tokens.map((token) => (
                    <tr
                      key={token.id}
                      className="border-t hover:bg-bg-subtle/50"
                      data-testid={`token-row-${token.id}`}
                    >
                      <td className="px-4 py-2 font-medium">
                        <div
                          className="max-w-[220px] truncate"
                          title={token.name}
                        >
                          {token.name}
                        </div>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs whitespace-nowrap">
                        {token.token_prefix}…
                      </td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        {tokenStatusBadge(token, t)}
                      </td>
                      <td className="px-4 py-2 text-text-secondary whitespace-nowrap">
                        {formatExpiresAt(token.expires_at, t)}
                      </td>
                      <td className="px-4 py-2 text-text-secondary">
                        <div>{formatLastUsed(token.last_used_at, t)}</div>
                        {token.last_used_ip && (
                          <div
                            className="max-w-[160px] truncate text-xs"
                            title={token.last_used_ip}
                          >
                            {t("tokens.usedFrom", { ip: token.last_used_ip })}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right tabular-nums whitespace-nowrap">
                        {token.use_count}
                      </td>
                      <td className="px-4 py-2 text-right">
                        {!token.revoked_at && !isExpired(token) && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setRevokeCandidate(token)}
                            disabled={busyId === token.id}
                            leftIcon={<TrashIcon size={14} />}
                          >
                            {t("tokens.table.revokeAction")}
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile: cada token vira um cartão — sem scroll horizontal. */}
            <div className="space-y-3 p-4 md:hidden">
              {tokens.map((token) => (
                <div
                  key={token.id}
                  className="rounded-xl border border-border bg-surface p-4"
                  data-testid={`token-card-${token.id}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div
                        className="truncate font-medium text-text"
                        title={token.name}
                      >
                        {token.name}
                      </div>
                      <div className="font-mono text-xs text-text-secondary">
                        {token.token_prefix}…
                      </div>
                    </div>
                    {tokenStatusBadge(token, t)}
                  </div>
                  <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                    <div>
                      <dt className="text-text-secondary">{t("tokens.table.columns.expires")}</dt>
                      <dd className="text-text">
                        {formatExpiresAt(token.expires_at, t)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-text-secondary">{t("tokens.table.columns.uses")}</dt>
                      <dd className="text-text tabular-nums">
                        {token.use_count}
                      </dd>
                    </div>
                    <div className="col-span-2">
                      <dt className="text-text-secondary">{t("tokens.table.columns.lastUsed")}</dt>
                      <dd className="text-text">
                        {formatLastUsed(token.last_used_at, t)}
                        {token.last_used_ip && (
                          <span
                            className="block truncate text-text-secondary"
                            title={token.last_used_ip}
                          >
                            {t("tokens.usedFrom", { ip: token.last_used_ip })}
                          </span>
                        )}
                      </dd>
                    </div>
                  </dl>
                  {!token.revoked_at && !isExpired(token) && (
                    <div className="mt-3 flex justify-end">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setRevokeCandidate(token)}
                        disabled={busyId === token.id}
                        leftIcon={<TrashIcon size={14} />}
                      >
                        {t("tokens.table.revokeAction")}
                      </Button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      <CreateTokenModal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={async (_raw, token) => {
          setFeedback({
            type: "success",
            message: t("tokens.feedback.created", { name: token.name }),
          })
          await refetch()
        }}
      />

      <ConfirmDialog
        open={!!revokeCandidate}
        title={t("tokens.revokeDialog.title")}
        description={
          revokeCandidate
            ? t("tokens.revokeDialog.description", { name: revokeCandidate.name })
            : ""
        }
        confirmLabel={t("tokens.table.revokeAction")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="danger"
        loading={busyId === revokeCandidate?.id}
        onConfirm={handleRevoke}
        onClose={() => setRevokeCandidate(null)}
      />
    </div>
  )
}

// ── Modal de criação ────────────────────────────────────────────────

interface CreateTokenModalProps {
  open: boolean
  onClose: () => void
  onCreated: (raw: string, token: ApiToken) => Promise<void> | void
}

const CreateTokenModal: React.FC<CreateTokenModalProps> = ({
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

  // Reset state when modal closes (clear secrets from memory).
  useEffect(() => {
    if (!open) {
      const resetTimer = setTimeout(() => {
        setName("")
        setPreset("90d")
        setScopes(null)
        setCreatedRaw(null)
        setCopied(false)
        setError(null)
      }, 200)
      return () => clearTimeout(resetTimer)
    }
  }, [open])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) {
      setError(t("tokens.createModal.errors.nameRequired"))
      return
    }
    setCreating(true)
    setError(null)
    try {
      const expiresAt = expiryFromPreset(preset)
      const result = await api.createApiToken({
        name: name.trim(),
        expires_at: expiresAt,
        is_eternal: expiresAt === null,
        scopes: scopes && scopes.length > 0 ? scopes : null,
      })
      setCreatedRaw(result.token)
      await onCreated(result.token, result.api_token)
    } catch (err) {
      setError(err instanceof Error ? err.message : t("tokens.createModal.errors.createFailed"))
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
      // Browser sem clipboard — fallback: select text manualmente.
    }
  }

  const isEternal = preset === "never"

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={createdRaw ? t("tokens.createModal.titleCreated") : t("tokens.createModal.titleNew")}
      size="md"
      closeOnOverlayClick={!createdRaw}
      closeOnEscape={!createdRaw}
    >
      {createdRaw ? (
        <div className="space-y-4">
          <Notice variant="warning">
            <div className="font-semibold mb-1">{t("tokens.createModal.copyNow")}</div>
            <div className="text-sm">
              <Trans i18nKey="tokens.createModal.copyNowDescription" t={t} components={{ strong: <strong /> }} />
            </div>
          </Notice>

          <div>
            <label className="text-xs font-medium uppercase tracking-wider text-text-secondary">
              {t("tokens.createModal.tokenLabel")}
            </label>
            <div className="mt-1 flex items-center gap-2 rounded border bg-bg-subtle p-3 font-mono text-sm break-all">
              <span className="flex-1" data-testid="created-raw-token">
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
                {copied ? t("tokens.createModal.copied") : t("tokens.createModal.copy")}
              </Button>
            </div>
          </div>

          <div className="text-sm text-text-secondary">
            {t("tokens.createModal.useInHeader")}
            <pre className="mt-1 rounded bg-bg-subtle p-2 font-mono text-xs">
              Authorization: Bearer {createdRaw.slice(0, 12)}…
            </pre>
          </div>

          <div className="flex items-center justify-end gap-3">
            <span className="text-sm text-text-secondary">{t("tokens.createModal.closeToFinish")}</span>
            <Button onClick={onClose}>{t("common:actions.close")}</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label
              htmlFor="token-name"
              className="text-xs font-medium uppercase tracking-wider text-text-secondary"
            >
              {t("tokens.createModal.nameLabel")}
            </label>
            <Input
              id="token-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("tokens.createModal.namePlaceholder")}
              maxLength={100}
              autoFocus
              required
              // Impede que gerenciadores de senha (1Password, LastPass, Bitwarden)
              // tratem o campo como credencial e roubem o foco a cada tecla.
              autoComplete="off"
              data-1p-ignore="true"
              data-lpignore="true"
              data-form-type="other"
            />
            <p className="mt-1 text-xs text-text-secondary">
              {t("tokens.createModal.nameHelp")}
            </p>
          </div>

          <div>
            <label
              htmlFor="token-expiry"
              className="text-xs font-medium uppercase tracking-wider text-text-secondary"
            >
              {t("tokens.createModal.expiryLabel")}
            </label>
            <select
              id="token-expiry"
              value={preset}
              onChange={(e) => setPreset(e.target.value as ExpiryPreset)}
              className="mt-1 block w-full rounded-md border border-border bg-bg px-3 py-2 text-sm focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500"
            >
              {EXPIRY_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
            {isEternal && (
              <Notice variant="warning" className="mt-2">
                <div className="flex items-start gap-2">
                  <AlertTriangleIcon size={16} className="mt-0.5 shrink-0" />
                  <div className="text-sm">
                    <strong>{t("tokens.createModal.eternalWarningTitle")}</strong>
                    {t("tokens.createModal.eternalWarningDescription")}
                  </div>
                </div>
              </Notice>
            )}
          </div>

          <div>
            <label className="text-xs font-medium uppercase tracking-wider text-text-secondary">
              {t("tokens.createModal.scopesLabel")}
            </label>
            <div className="mt-1">
              <ScopeSelector
                value={scopes}
                onChange={setScopes}
                disabled={creating}
              />
            </div>
          </div>

          {error && <Notice variant="danger">{error}</Notice>}

          <div className="flex justify-end gap-2">
            <Button variant="ghost" type="button" onClick={onClose}>
              {t("common:actions.cancel")}
            </Button>
            <Button type="submit" disabled={creating}>
              {creating ? t("tokens.createModal.submitting") : t("tokens.createModal.submit")}
            </Button>
          </div>
        </form>
      )}
    </Modal>
  )
}

export default TokensPage
