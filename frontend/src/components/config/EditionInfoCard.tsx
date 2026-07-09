"use client"

import type React from "react"
import { CrownIcon, InfoIcon } from "lucide-react"
import { Trans, useTranslation } from "react-i18next"
import { useEdition } from "@/contexts/EditionContext"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { formatDate } from "@/lib/intl"

// Avisa com esta antecedência antes do vencimento da licença.
const EXPIRY_WARN_DAYS = 14

function daysUntil(iso: string | null): number | null {
  if (!iso) return null
  const ms = new Date(iso).getTime()
  if (Number.isNaN(ms)) return null
  return Math.ceil((ms - Date.now()) / 86_400_000)
}

/**
 * Surface compacta da edição/licença corrente.
 *
 * Mostra Community/Enterprise + plano + features licenciadas, e avisa quando a
 * licença está perto de vencer. NB: o backend faz fail-closed — uma licença já
 * expirada resolve para Community (sem expires_at), então o aviso "expirada" só
 * aparece numa sessão deixada aberta após o vencimento (a próxima chamada à API
 * já reverteria para Community).
 */
export const EditionInfoCard: React.FC = () => {
  const { t } = useTranslation("config")
  const { isEnterprise, plan, features, expiresAt, expiredInGrace, loading } = useEdition()
  // Skeleton enquanto carrega — evita layout shift (a seção não some/reaparece).
  if (loading) return <div className="h-6 w-48 animate-pulse rounded bg-surface-tertiary" />

  const days = daysUntil(expiresAt)
  const expired = days != null && days < 0 && !expiredInGrace
  const expiringSoon = days != null && days >= 0 && days <= EXPIRY_WARN_DAYS

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-sm">
        <Badge variant={isEnterprise ? "primary" : "outline"} size="sm" className="gap-1.5">
          {isEnterprise ? <CrownIcon size={12} /> : <InfoIcon size={12} />}
          {isEnterprise ? t("edition.enterprise") : t("edition.community")}
        </Badge>
        {plan && (
          <span className="text-text-secondary">
            <Trans i18nKey="edition.plan" t={t} values={{ plan }} components={{ strong: <strong className="text-text" /> }} />
          </span>
        )}
        {isEnterprise && (
          <span className="text-text-tertiary">{t("edition.featuresLicensed", { count: features.length })}</span>
        )}
        {expiresAt && !expired && (
          <span className="text-text-tertiary">
            {t("edition.expiresOn", {
              date: formatDate(expiresAt),
              daysSuffix: days != null ? t("edition.expiresOnDaysSuffix", { days }) : "",
            })}
          </span>
        )}
      </div>

      {expiredInGrace && (
        <Notice variant="warning" title={t("edition.graceTitle")}>
          <Trans i18nKey="edition.graceBody" t={t} components={{ strong: <strong /> }} />
        </Notice>
      )}
      {expired && (
        <Notice variant="warning" title={t("edition.expiredTitle")}>
          {t("edition.expiredBody")}
        </Notice>
      )}
      {expiringSoon && (
        <Notice variant="warning" title={t("edition.expiringSoonTitle")}>
          {t("edition.expiringSoonBody", { days, date: formatDate(expiresAt as string) })}
        </Notice>
      )}
    </div>
  )
}

export default EditionInfoCard
