"use client"

import type React from "react"
import { useEffect, useState } from "react"
import { CrownIcon, KeyRoundIcon, ShieldCheckIcon } from "lucide-react"
import { Trans, useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { useEdition } from "@/contexts/EditionContext"
import { formatDate, formatDateTime } from "@/lib/intl"
import * as api from "@/services/api"
import type { LicenseStatus } from "@/types"

type Feedback = { type: "success" | "danger"; message: string } | null

/**
 * Ativação de licença Enterprise. O admin cola o token assinado; o backend
 * valida OFFLINE contra o keyring público e persiste o token CIFRADO no banco
 * (``license_config``). A partir daí o deploy lê a licença DB-first — sobrevive a
 * reinícios, sem editar ``.env``. Desativar limpa o token e reverte para Community.
 */
export const LicenseActivationForm: React.FC = () => {
  const { t } = useTranslation("config")
  const SOURCE_LABEL: Record<LicenseStatus["source"], string> = {
    database: t("licensing.source.database"),
    environment: t("licensing.source.environment"),
    none: t("licensing.source.none"),
  }
  const { refresh: refreshEdition } = useEdition()
  const [status, setStatus] = useState<LicenseStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [token, setToken] = useState("")
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState<Feedback>(null)

  async function load() {
    setLoading(true)
    try {
      setStatus(await api.getLicenseStatus())
    } catch {
      setStatus(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  async function activate(e: React.FormEvent) {
    e.preventDefault()
    if (!token.trim()) return
    setBusy(true)
    setFeedback(null)
    try {
      const next = await api.activateLicense(token.trim())
      setStatus(next)
      setToken("")
      setFeedback({
        type: "success",
        message:
          next.edition === "enterprise"
            ? t("licensing.activateSuccessEnterprise", { plan: next.plan ?? t("licensing.unknownPlan") })
            : t("licensing.activateSuccessCommunityOnly"),
      })
      await refreshEdition() // atualiza os gates de feature em toda a UI
    } catch (err) {
      setFeedback({
        type: "danger",
        message: err instanceof Error ? err.message : t("licensing.activateError"),
      })
    } finally {
      setBusy(false)
    }
  }

  async function deactivate() {
    setBusy(true)
    setFeedback(null)
    try {
      const next = await api.deactivateLicense()
      setStatus(next)
      setFeedback({ type: "success", message: t("licensing.deactivateSuccess") })
      await refreshEdition()
    } catch (err) {
      setFeedback({
        type: "danger",
        message: err instanceof Error ? err.message : t("licensing.deactivateError"),
      })
    } finally {
      setBusy(false)
    }
  }

  const isEnterprise = status?.edition === "enterprise"
  const fromDb = status?.source === "database"

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <KeyRoundIcon size={18} className="text-text-secondary" />
          <CardTitle>{t("licensing.cardTitle")}</CardTitle>
        </div>
        <CardDescription>
          <Trans i18nKey="licensing.cardDescription" t={t} components={{ strong: <strong /> }} />
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-5">
        {feedback && <Notice variant={feedback.type}>{feedback.message}</Notice>}
        {status?.expired_in_grace && (
          <Notice variant="warning" title={t("licensing.graceTitle")}>
            {t("licensing.graceBody")}
          </Notice>
        )}

        {/* Estado atual */}
        {loading ? (
          <div className="h-6 w-56 animate-pulse rounded bg-surface-tertiary" />
        ) : (
          <div className="space-y-2 rounded-lg border border-border bg-surface-secondary/40 p-4">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
              <Badge variant={isEnterprise ? "primary" : "outline"} size="sm" className="gap-1.5">
                {isEnterprise ? <CrownIcon size={12} /> : <ShieldCheckIcon size={12} />}
                {isEnterprise ? t("licensing.edition.enterprise") : t("licensing.edition.community")}
              </Badge>
              {status?.plan && (
                <span className="text-text-secondary">
                  <Trans i18nKey="licensing.plan" t={t} values={{ plan: status.plan }} components={{ strong: <strong className="text-text" /> }} />
                </span>
              )}
              {status && (
                <span className="text-text-tertiary">
                  {t("licensing.origin", { source: SOURCE_LABEL[status.source] })}
                </span>
              )}
              {status?.expires_at && (
                <span className="text-text-tertiary">
                  {t("licensing.expiresOn", { date: formatDate(status.expires_at) })}
                </span>
              )}
            </div>
            {isEnterprise && (status?.features?.length ?? 0) > 0 && (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {status!.features.map((f) => (
                  <Badge key={f} variant="outline" size="sm" className="font-mono">
                    {f}
                  </Badge>
                ))}
              </div>
            )}
            {status?.activated_at && (
              <p className="text-xs text-text-tertiary">
                {t("licensing.activatedOn", { date: formatDateTime(status.activated_at) })}
                {status.activated_by ? t("licensing.activatedBy", { name: status.activated_by }) : ""}.
              </p>
            )}
          </div>
        )}

        {/* Ativação */}
        <form onSubmit={activate} className="space-y-3">
          <Textarea
            label={t("licensing.form.label")}
            placeholder={t("licensing.form.placeholder")}
            value={token}
            onChange={(e) => setToken(e.target.value)}
            rows={4}
            spellCheck={false}
            className="font-mono"
            helperText={t("licensing.form.helper")}
          />
          <div className="flex flex-wrap gap-2">
            <Button type="submit" size="sm" loading={busy} disabled={busy || !token.trim()}>
              {t("licensing.form.activate")}
            </Button>
            {fromDb && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => void deactivate()}
              >
                {t("licensing.form.deactivate")}
              </Button>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

export default LicenseActivationForm
