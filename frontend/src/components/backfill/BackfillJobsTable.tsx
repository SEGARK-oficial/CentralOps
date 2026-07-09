"use client"

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { cn } from "@/lib/utils"
import { formatDate, formatRelativeDate } from "@/lib/utils"
import { formatNumber } from "@/lib/intl"
import { usePermission } from "@/hooks/usePermission"
import type { BackfillJob, BackfillJobStatus } from "@/types"
import { BackfillJobDetailDrawer } from "./BackfillJobDetailDrawer"

const STATUS_VARIANT: Record<BackfillJobStatus, "default" | "primary" | "success" | "danger" | "warning"> = {
  pending: "default",
  running: "primary",
  completed: "success",
  failed: "danger",
  cancelled: "default",
}

const thCls = "px-4 py-3 text-left text-xs font-semibold text-text-secondary uppercase tracking-wider"
const tdCls = "px-4 py-3 text-sm"

interface BackfillJobsTableProps {
  items: BackfillJob[]
  isLoading: boolean
  error: Error | null
  onCancel: (jobId: string) => Promise<BackfillJob>
}

export const BackfillJobsTable: React.FC<BackfillJobsTableProps> = ({
  items,
  isLoading,
  error,
  onCancel,
}) => {
  const { t } = useTranslation("config")
  const canWrite = usePermission("integration.write")
  const [selectedJob, setSelectedJob] = useState<BackfillJob | null>(null)
  const [cancelTarget, setCancelTarget] = useState<BackfillJob | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)

  const handleConfirmCancel = async () => {
    if (!cancelTarget) return
    try {
      setCancelling(true)
      setCancelError(null)
      await onCancel(cancelTarget.id)
      setCancelTarget(null)
    } catch (err) {
      const message = err instanceof Error ? err.message : t("backfill.table.cancelError")
      setCancelError(message)
    } finally {
      setCancelling(false)
    }
  }

  if (isLoading) return <LoadingSpinner size="md" text={t("backfill.table.loading")} className="py-10" />

  if (error) {
    return (
      <Notice variant="danger" title={t("backfill.table.loadError")}>
        {error.message}
      </Notice>
    )
  }

  if (items.length === 0) {
    return (
      <EmptyState
        title={t("backfill.table.emptyTitle")}
        description={t("backfill.table.emptyDescription")}
      />
    )
  }

  return (
    <>
      {cancelError && (
        <Notice variant="danger" title={t("backfill.table.cancelErrorTitle")}>
          {cancelError}
        </Notice>
      )}

      <div
        data-testid="backfill-jobs-table"
        className="overflow-x-auto rounded-lg border border-border"
      >
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-surface-tertiary">
              <th className={thCls}>{t("backfill.table.id")}</th>
              <th className={thCls}>{t("backfill.table.streams")}</th>
              <th className={thCls}>{t("backfill.table.window")}</th>
              <th className={thCls}>{t("backfill.table.status")}</th>
              <th className={thCls}>{t("backfill.table.progress")}</th>
              <th className={thCls}>{t("backfill.table.events")}</th>
              <th className={thCls}>{t("backfill.table.requested")}</th>
              <th className={thCls}>{t("backfill.table.actions")}</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {items.map((job) => (
              <tr
                key={job.id}
                data-testid={`backfill-row-${job.id}`}
                className="hover:bg-surface-tertiary/50"
              >
                {/* ID truncado */}
                <td className={cn(tdCls, "font-mono text-xs")}>
                  <span title={job.id}>{job.id.slice(0, 8)}…</span>
                </td>

                {/* Streams */}
                <td className={tdCls}>
                  <div className="flex flex-wrap gap-1">
                    {job.streams.map((s) => (
                      <Badge key={s} variant="default" size="sm">
                        {s}
                      </Badge>
                    ))}
                  </div>
                </td>

                {/* Janela */}
                <td className={cn(tdCls, "whitespace-nowrap text-xs text-text-secondary")}>
                  {formatDate(job.from_ts)} → {formatDate(job.to_ts)}
                </td>

                {/* Status */}
                <td className={tdCls}>
                  <Badge
                    variant={STATUS_VARIANT[job.status]}
                    size="sm"
                    className={cn(job.status === "cancelled" && "line-through")}
                  >
                    {t(`backfill.status.${job.status}`)}
                  </Badge>
                </td>

                {/* Progresso */}
                <td className={cn(tdCls, "min-w-[120px]")}>
                  <div className="flex items-center gap-2">
                    <div
                      className="h-2 w-20 overflow-hidden rounded-full bg-surface-tertiary"
                      role="progressbar"
                      aria-valuenow={job.progress_pct}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label={t("backfill.table.progressAriaLabel", { percent: job.progress_pct })}
                    >
                      <div
                        className={cn(
                          "h-full rounded-full transition-all duration-300",
                          job.progress_pct >= 100
                            ? "bg-success-500"
                            : job.status === "failed"
                              ? "bg-danger-500"
                              : "bg-primary-500",
                        )}
                        style={{ width: `${Math.min(100, job.progress_pct)}%` }}
                      />
                    </div>
                    <span className="text-xs text-text-secondary">{job.progress_pct}%</span>
                  </div>
                </td>

                {/* Eventos */}
                <td className={cn(tdCls, "text-xs text-text-secondary whitespace-nowrap")}>
                  {formatNumber(job.events_collected)} /{" "}
                  {formatNumber(job.events_dispatched)}
                </td>

                {/* Solicitado em */}
                <td className={cn(tdCls, "text-xs text-text-secondary whitespace-nowrap")}>
                  {formatRelativeDate(job.requested_at)}
                </td>

                {/* Ações */}
                <td className={cn(tdCls, "whitespace-nowrap")}>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="xs"
                      onClick={() => setSelectedJob(job)}
                    >
                      {t("backfill.table.details")}
                    </Button>
                    {canWrite && (job.status === "pending" || job.status === "running") && (
                      <Button
                        variant="danger"
                        size="xs"
                        data-testid={`cancel-backfill-${job.id}`}
                        onClick={() => setCancelTarget(job)}
                      >
                        {t("backfill.table.cancel")}
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Drawer de detalhes */}
      {selectedJob && (
        <BackfillJobDetailDrawer
          job={selectedJob}
          open={!!selectedJob}
          onClose={() => setSelectedJob(null)}
          onCancel={
            canWrite && (selectedJob.status === "pending" || selectedJob.status === "running")
              ? async () => {
                  await onCancel(selectedJob.id)
                  setSelectedJob(null)
                }
              : undefined
          }
        />
      )}

      {/* Confirm cancelamento */}
      <ConfirmDialog
        open={!!cancelTarget}
        title={t("backfill.cancelDialog.title")}
        description={t("backfill.cancelDialog.description", { jobId: cancelTarget?.id.slice(0, 8) })}
        confirmLabel={t("backfill.cancelDialog.confirm")}
        cancelLabel={t("backfill.cancelDialog.back")}
        confirmVariant="danger"
        loading={cancelling}
        onConfirm={() => void handleConfirmCancel()}
        onClose={() => {
          if (!cancelling) setCancelTarget(null)
        }}
      />
    </>
  )
}

export default BackfillJobsTable
