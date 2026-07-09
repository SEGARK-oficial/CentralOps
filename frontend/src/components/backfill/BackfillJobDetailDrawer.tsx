"use client"

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import type { TFunction } from "i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { cn } from "@/lib/utils"
import { formatDate, formatRelativeDate } from "@/lib/utils"
import { formatNumber } from "@/lib/intl"
import type { BackfillJob, BackfillJobStatus } from "@/types"

function statusLabel(status: BackfillJobStatus, t: TFunction): string {
  return t(`backfill.status.${status}`)
}

const STATUS_VARIANT: Record<BackfillJobStatus, "default" | "primary" | "success" | "danger" | "warning"> = {
  pending: "default",
  running: "primary",
  completed: "success",
  failed: "danger",
  cancelled: "default",
}

interface FieldRowProps {
  label: string
  value: React.ReactNode
}

const FieldRow: React.FC<FieldRowProps> = ({ label, value }) => (
  <div className="flex flex-col gap-0.5 sm:flex-row sm:items-start">
    <dt className="min-w-[160px] text-xs font-semibold uppercase tracking-wide text-text-secondary">
      {label}
    </dt>
    <dd className="text-sm text-text">{value ?? "—"}</dd>
  </div>
)

interface BackfillJobDetailDrawerProps {
  job: BackfillJob
  open: boolean
  onClose: () => void
  onCancel?: () => Promise<void>
}

export const BackfillJobDetailDrawer: React.FC<BackfillJobDetailDrawerProps> = ({
  job,
  open,
  onClose,
  onCancel,
}) => {
  const { t } = useTranslation("config")
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [cancelling, setCancelling] = useState(false)

  const canCancel = !!onCancel

  const handleCancel = async () => {
    if (!onCancel) return
    try {
      setCancelling(true)
      await onCancel()
      setConfirmOpen(false)
      onClose()
    } finally {
      setCancelling(false)
    }
  }

  const lastActivityAt =
    job.cancelled_at ?? job.finished_at ?? job.started_at ?? job.requested_at

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        title={t("backfill.detailDrawer.title")}
        size="lg"
      >
        <div className="flex flex-col gap-5">
          {/* Status principal */}
          <div className="flex items-center gap-3">
            <Badge
              variant={STATUS_VARIANT[job.status]}
              size="lg"
              className={cn(job.status === "cancelled" && "line-through")}
            >
              {statusLabel(job.status, t)}
            </Badge>
            <span className="text-xs text-text-secondary">
              {t("backfill.detailDrawer.lastUpdate", { when: formatRelativeDate(lastActivityAt) })}
            </span>
          </div>

          {/* Progresso */}
          <div className="flex flex-col gap-1">
            <div className="flex items-center justify-between text-xs text-text-secondary">
              <span>{t("backfill.detailDrawer.progress")}</span>
              <span>{job.progress_pct}%</span>
            </div>
            <div
              className="h-2 w-full overflow-hidden rounded-full bg-surface-tertiary"
              role="progressbar"
              aria-valuenow={job.progress_pct}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={t("backfill.detailDrawer.progressAriaLabel", { percent: job.progress_pct })}
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
          </div>

          {/* Campos */}
          <dl className="flex flex-col gap-3 rounded-lg border border-border bg-surface-tertiary/30 p-4">
            <FieldRow label={t("backfill.detailDrawer.fields.id")} value={<span className="font-mono text-xs">{job.id}</span>} />
            <FieldRow
              label={t("backfill.detailDrawer.fields.streams")}
              value={
                <div className="flex flex-wrap gap-1">
                  {job.streams.map((s) => (
                    <Badge key={s} variant="default" size="sm">
                      {s}
                    </Badge>
                  ))}
                </div>
              }
            />
            <FieldRow
              label={t("backfill.detailDrawer.fields.window")}
              value={
                <span className="text-xs">
                  {formatDate(job.from_ts)} → {formatDate(job.to_ts)}
                </span>
              }
            />
            <FieldRow
              label={t("backfill.detailDrawer.fields.eventsCollected")}
              value={formatNumber(job.events_collected)}
            />
            <FieldRow
              label={t("backfill.detailDrawer.fields.eventsDispatched")}
              value={formatNumber(job.events_dispatched)}
            />
            <FieldRow
              label={t("backfill.detailDrawer.fields.requestedAt")}
              value={formatDate(job.requested_at)}
            />
            <FieldRow
              label={t("backfill.detailDrawer.fields.startedAt")}
              value={job.started_at ? formatDate(job.started_at) : "—"}
            />
            <FieldRow
              label={t("backfill.detailDrawer.fields.finishedAt")}
              value={job.finished_at ? formatDate(job.finished_at) : "—"}
            />
            {job.cancelled_at && (
              <FieldRow
                label={t("backfill.detailDrawer.fields.cancelledAt")}
                value={formatDate(job.cancelled_at)}
              />
            )}
          </dl>

          {/* Erro */}
          {job.last_error && (
            <Notice variant="danger" title={t("backfill.detailDrawer.lastErrorTitle")}>
              <code className="block whitespace-pre-wrap break-all text-xs">
                {job.last_error}
              </code>
            </Notice>
          )}

          {/* Ações */}
          <div className="flex justify-end gap-3 pt-1">
            <Button variant="outline" onClick={onClose}>
              {t("backfill.detailDrawer.close")}
            </Button>
            {canCancel && (
              <Button
                variant="danger"
                onClick={() => setConfirmOpen(true)}
                disabled={cancelling}
              >
                {t("backfill.detailDrawer.cancel")}
              </Button>
            )}
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={confirmOpen}
        title={t("backfill.cancelDialog.title")}
        description={t("backfill.cancelDialog.description", { jobId: job.id.slice(0, 8) })}
        confirmLabel={t("backfill.cancelDialog.confirm")}
        cancelLabel={t("backfill.cancelDialog.back")}
        confirmVariant="danger"
        loading={cancelling}
        onConfirm={() => void handleCancel()}
        onClose={() => {
          if (!cancelling) setConfirmOpen(false)
        }}
      />
    </>
  )
}

export default BackfillJobDetailDrawer
