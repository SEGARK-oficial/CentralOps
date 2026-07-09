/**
 * RouteAuditPanel — painel de auditoria por rota com rollback.
 *
 * Carrega routeAudit(id) e exibe a trilha (action/actor/created_at/snapshot).
 * Cada entrada tem botão "Reverter" que chama rollbackRoute com ConfirmDialog.
 *
 * A11y: role region + aria-live para estado de carregamento; botão de rollback
 * com aria-label explícito.
 */

import type React from "react"
import { useCallback, useMemo, useState } from "react"
import { useTranslation } from "react-i18next"
import { HistoryIcon, RotateCcwIcon } from "lucide-react"
import * as api from "@/services/api"
import { useAsyncResource } from "@/hooks/useAsyncResource"
import { SkeletonCard } from "@/components/ui/Skeleton"
import { ErrorState } from "@/components/ui/ErrorState"
import { Button } from "@/components/ui/Button/Button"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { formatDateTime } from "@/lib/intl"
import type { RouteAudit } from "@/types"

interface RouteAuditPanelProps {
  routeId: string
  routeName: string
  /** Chamado após rollback bem-sucedido para recarregar a lista de rotas. */
  onRolledBack?: () => void
}

export const RouteAuditPanel: React.FC<RouteAuditPanelProps> = ({
  routeId,
  routeName,
  onRolledBack,
}) => {
  const { t } = useTranslation("routing")
  const [rollbackTarget, setRollbackTarget] = useState<RouteAudit | null>(null)
  const [rolling, setRolling] = useState(false)
  const [toast, setToast] = useState<{ type: "success" | "error"; message: string } | null>(null)

  const ACTION_LABELS: Record<string, string> = useMemo(
    () => ({
      created: t("auditPanel.actions.created"),
      updated: t("auditPanel.actions.updated"),
      deleted: t("auditPanel.actions.deleted"),
      rolled_back: t("auditPanel.actions.rolled_back"),
      reordered: t("auditPanel.actions.reordered"),
    }),
    [t],
  )

  const loader = useCallback(() => api.routeAudit(routeId), [routeId])
  const { data: entries, loading, error, reload } = useAsyncResource<RouteAudit[]>(loader)

  const handleRollback = async () => {
    if (!rollbackTarget) return
    setRolling(true)
    try {
      await api.rollbackRoute(routeId, rollbackTarget.id)
      setToast({ type: "success", message: t("auditPanel.rollbackSuccess", { name: routeName, date: formatDateTime(rollbackTarget.created_at) }) })
      setRollbackTarget(null)
      reload()
      onRolledBack?.()
    } catch (err) {
      setToast({ type: "error", message: err instanceof Error ? err.message : t("auditPanel.rollbackError") })
    } finally {
      setRolling(false)
    }
  }

  return (
    <section aria-label={t("auditPanel.sectionAria", { name: routeName })} className="space-y-4">
      <div className="flex items-center justify-between">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-text">
          <HistoryIcon size={16} aria-hidden="true" />
          {t("auditPanel.title")}
        </h3>
        <Button variant="outline" size="sm" onClick={reload} disabled={loading}>
          {t("common:actions.refresh")}
        </Button>
      </div>

      {toast && (
        <Notice
          variant={toast.type === "success" ? "success" : "danger"}
          title={toast.type === "success" ? t("auditPanel.feedbackOkTitle") : t("auditPanel.feedbackErrorTitle")}
        >
          {toast.message}
        </Notice>
      )}

      {loading && (
        <div role="status" aria-label={t("auditPanel.loadingAria")} className="space-y-2">
          <SkeletonCard lines={2} />
          <SkeletonCard lines={2} />
        </div>
      )}

      {!loading && error && (
        <ErrorState
          title={t("auditPanel.loadErrorTitle")}
          message={error.message}
          onRetry={reload}
        />
      )}

      {!loading && !error && entries && entries.length === 0 && (
        <p className="text-sm text-text-secondary">{t("auditPanel.empty")}</p>
      )}

      {!loading && !error && entries && entries.length > 0 && (
        <div
          aria-live="polite"
          className="divide-y divide-border rounded-lg border border-border"
        >
          {entries.map((entry) => (
            <div key={entry.id} className="flex flex-wrap items-start justify-between gap-3 p-3">
              <div className="min-w-0 space-y-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={entry.action === "deleted" ? "danger" : entry.action === "created" ? "success" : "default"}>
                    {ACTION_LABELS[entry.action] ?? entry.action}
                  </Badge>
                  {entry.actor && (
                    <span className="text-xs text-text-secondary">{entry.actor}</span>
                  )}
                  <span className="text-xs text-text-tertiary">{formatDateTime(entry.created_at)}</span>
                </div>
                {entry.snapshot && Object.keys(entry.snapshot).length > 0 && (
                  <details className="text-xs text-text-tertiary">
                    <summary className="cursor-pointer hover:text-text-secondary">{t("auditPanel.viewSnapshot")}</summary>
                    <pre className="mt-1 max-h-32 overflow-auto rounded bg-surface-tertiary p-2 font-mono text-xs">
                      {JSON.stringify(entry.snapshot, null, 2)}
                    </pre>
                  </details>
                )}
              </div>
              {entry.action !== "deleted" && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setRollbackTarget(entry)}
                  leftIcon={<RotateCcwIcon size={14} />}
                  aria-label={t("auditPanel.rollbackAria", { date: formatDateTime(entry.created_at) })}
                >
                  {t("auditPanel.rollback")}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={rollbackTarget !== null}
        title={t("auditPanel.rollbackDialogTitle")}
        description={
          rollbackTarget
            ? t("auditPanel.rollbackDialogDescription", {
                name: routeName,
                date: formatDateTime(rollbackTarget.created_at),
                action: ACTION_LABELS[rollbackTarget.action] ?? rollbackTarget.action,
              })
            : ""
        }
        confirmLabel={t("auditPanel.rollback")}
        confirmVariant="danger"
        loading={rolling}
        onConfirm={() => void handleRollback()}
        onClose={() => !rolling && setRollbackTarget(null)}
      />
    </section>
  )
}
