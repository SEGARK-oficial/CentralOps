"use client"

import type React from "react"
import { useState } from "react"
import { RefreshCwIcon, ZapIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { formatDateTime } from "@/lib/intl"
import type { EntraSyncStatus, EntraSyncSummary } from "@/types"

// ── helpers ────────────────────────────────────────────────────────────

type SyncFeedback = { type: "success" | "error" | "info"; message: string } | null

/** Formata timestamp ISO UTC em data/hora local. Retorna o rótulo "Nunca" se null. */
function formatSyncAt(isoString: string | null | undefined, t: TFunction): string {
  if (!isoString) return t("entraSync.never")
  try {
    return formatDateTime(isoString)
  } catch {
    return isoString
  }
}

/** Badge colorido por status de sync. */
function SyncStatusBadge({
  status,
  lockActive,
  t,
}: {
  status?: string | null
  lockActive: boolean
  t: TFunction
}) {
  if (lockActive || status === "running") {
    return (
      <Badge variant="warning" dot aria-label={t("entraSync.status.inProgressAriaLabel")}>
        {t("entraSync.status.inProgress")}
      </Badge>
    )
  }
  if (status === "ok") {
    return (
      <Badge variant="success" dot aria-label={t("entraSync.status.okAriaLabel")}>
        {t("entraSync.status.ok")}
      </Badge>
    )
  }
  if (status === "error") {
    return (
      <Badge variant="danger" dot aria-label={t("entraSync.status.errorAriaLabel")}>
        {t("entraSync.status.error")}
      </Badge>
    )
  }
  if (status === "partial") {
    return (
      <Badge variant="warning" dot aria-label={t("entraSync.status.partialAriaLabel")}>
        {t("entraSync.status.partial")}
      </Badge>
    )
  }
  // 'never' ou null
  return (
    <Badge variant="outline" aria-label={t("entraSync.status.neverAriaLabel")}>
      {t("entraSync.status.never")}
    </Badge>
  )
}

/** Linha de contadores do último sync (criados/atualizados/desativados). */
function SyncCounters({ summary, t }: { summary?: EntraSyncSummary | null; t: TFunction }) {
  if (!summary) return null
  return (
    <p className="text-sm text-text-secondary mt-1">
      {t("entraSync.counters.created")}{" "}
      <span className="font-semibold text-text">{summary.created}</span>
      {" / "}
      {t("entraSync.counters.updated")}{" "}
      <span className="font-semibold text-text">{summary.updated}</span>
      {" / "}
      {t("entraSync.counters.deactivated")}{" "}
      <span className="font-semibold text-text">{summary.deactivated}</span>
    </p>
  )
}

/** Lista colapsável de erros do último sync. Só renderiza se houver erros. */
function SyncErrorList({ errors, t }: { errors?: string[]; t: TFunction }) {
  const [expanded, setExpanded] = useState(false)

  if (!errors || errors.length === 0) return null

  return (
    <div className="mt-2">
      <button
        type="button"
        className="text-xs text-danger-600 underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-primary-500 rounded"
        aria-expanded={expanded}
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? t("entraSync.errors.hide") : t("entraSync.errors.showCount", { count: errors.length })}
      </button>
      {expanded && (
        <ul
          className="mt-1.5 space-y-1 pl-3 border-l-2 border-danger-200"
          role="list"
          aria-label={t("entraSync.errors.listAriaLabel")}
        >
          {errors.map((e, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: lista de erros de exibição
            <li key={i} className="text-xs text-danger-700 leading-relaxed">
              {e}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// ── props ──────────────────────────────────────────────────────────────

export interface EntraSyncPanelProps {
  syncStatus: EntraSyncStatus | null
  loadingStatus: boolean
  syncing: boolean
  feedback: SyncFeedback
  onSyncNow: () => Promise<unknown>
  onRefreshStatus: () => Promise<void>
}

/**
 * Painel de status de sincronização de usuários do Entra via Graph (Fase 2B).
 *
 * Mostra: último sync (timestamp), badge de status, contadores
 * (criados/atualizados/desativados), erros colapsáveis e botão "Sincronizar agora".
 *
 * Acessibilidade:
 * - A região tem role="status" + aria-live="polite" para anunciar atualizações.
 * - O botão "Sincronizar agora" é desabilitado quando o lock está ativo.
 * - Badge usa aria-label descritivo.
 */
export const EntraSyncPanel: React.FC<EntraSyncPanelProps> = ({
  syncStatus,
  loadingStatus,
  syncing,
  feedback,
  onSyncNow,
  onRefreshStatus,
}) => {
  const { t } = useTranslation("config")
  const lockActive = syncStatus?.lock_active ?? false
  const lastSyncAt = syncStatus?.last_sync_at
  const lastStatus = lockActive ? "running" : syncStatus?.last_sync_status
  const summary = syncStatus?.last_sync_summary

  return (
    <Card variant="outlined" padding="md" className="mt-6">
      <CardHeader>
        <CardTitle as="h4" className="text-base">
          <ZapIcon size={16} aria-hidden="true" className="text-primary-500" />
          {t("entraSync.cardTitle")}
        </CardTitle>
        <CardDescription>
          {t("entraSync.cardDescription")}
        </CardDescription>
      </CardHeader>

      <CardContent>
        {/* Feedback pós-clique no botão Sincronizar agora */}
        {feedback && (
          <Notice
            variant={feedback.type === "error" ? "danger" : feedback.type === "info" ? "info" : "success"}
            className="mb-3"
          >
            {feedback.message}
          </Notice>
        )}

        {/* Região de status — anuncia mudanças para leitores de tela */}
        <div
          role="status"
          aria-live="polite"
          aria-label={t("entraSync.statusRegionAriaLabel")}
          data-testid="entra-sync-status-region"
          className="space-y-2"
        >
          {loadingStatus ? (
            <p className="text-sm text-text-tertiary">{t("entraSync.loadingStatus")}</p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-3">
                <SyncStatusBadge status={lastStatus} lockActive={lockActive} t={t} />
                <span className="text-sm text-text-secondary">
                  {t("entraSync.lastSync")}{" "}
                  <span className="font-medium text-text">{formatSyncAt(lastSyncAt, t)}</span>
                </span>
              </div>

              <SyncCounters summary={summary} t={t} />
              <SyncErrorList errors={summary?.errors} t={t} />
            </>
          )}
        </div>

        {/* Ações */}
        <div className="flex flex-wrap items-center gap-2 mt-4">
          <Button
            type="button"
            size="sm"
            loading={syncing}
            disabled={lockActive || syncing}
            onClick={() => void onSyncNow()}
            aria-label={lockActive ? t("entraSync.syncNowAriaLabelRunning") : t("entraSync.syncNowAriaLabelIdle")}
          >
            {t("entraSync.syncNow")}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            loading={loadingStatus && !syncing}
            disabled={loadingStatus}
            onClick={() => void onRefreshStatus()}
            aria-label={t("entraSync.refreshAriaLabel")}
          >
            <RefreshCwIcon size={14} aria-hidden="true" />
            {t("entraSync.refresh")}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}

export default EntraSyncPanel
