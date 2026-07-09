/**
 * QuarantineTable
 * Tabela de entradas de quarentena com ações: Detalhes, Descartar (+ confirm),
 * Reprocessar (com gating por permissão + ConfirmDialog).
 */

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { InfoIcon, Trash2Icon, RefreshCwIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Notice } from "@/components/ui/Notice/Notice"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { usePermission } from "@/hooks/usePermission"
import type { HeaderCheckboxState } from "@/hooks/useBulkSelection"
import type { PaginationConfig, QuarantineDetail, QuarantineEntry, TableColumn } from "@/types"

type AnyRow = Record<string, unknown>
type TFn = ReturnType<typeof useTranslation>["t"]

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatRelativeTime(iso: string, t: TFn): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const diffS = Math.floor(diffMs / 1000)
  if (diffS < 60) return t("quarantine.table.relativeTime.now")
  const diffM = Math.floor(diffS / 60)
  if (diffM < 60) return t("quarantine.table.relativeTime.minutesAgo", { count: diffM })
  const diffH = Math.floor(diffM / 60)
  if (diffH < 24) return t("quarantine.table.relativeTime.hoursAgo", { count: diffH })
  const diffD = Math.floor(diffH / 24)
  return t("quarantine.table.relativeTime.daysAgo", { count: diffD })
}

function truncate(str: string | null | undefined, maxLen: number, emptyValue: string): string {
  if (!str) return emptyValue
  return str.length > maxLen ? `${str.slice(0, maxLen)}…` : str
}

const ERROR_KIND_VARIANT: Record<string, "danger" | "warning" | "default"> = {
  schema_error: "danger",
  missing_required: "danger",
  type_cast_failed: "warning",
  value_map_no_match: "warning",
  jmespath_eval_failed: "warning",
}

// ── Component ─────────────────────────────────────────────────────────────────

/**
 * PR #3: props opcionais para habilitar bulk-select. Quando ausentes, a
 * tabela renderiza sem coluna de checkbox (compat retro).
 */
export interface QuarantineTableSelectionProps {
  isSelected: (id: string) => boolean
  toggleOne: (id: string) => void
  toggleAllVisible: () => void
  headerCheckboxState: HeaderCheckboxState
}

interface QuarantineTableProps {
  items: QuarantineEntry[]
  total: number
  pagination: PaginationConfig
  onPaginationChange: (p: PaginationConfig) => void
  onDiscard: (id: string) => Promise<void>
  onReprocess: (id: string) => Promise<QuarantineEntry>
  onGetDetail: (id: string) => Promise<QuarantineDetail>
  onOpenDetail: (detail: QuarantineDetail) => void
  /** PR #3: quando passado, ativa coluna de bulk-select. */
  selection?: QuarantineTableSelectionProps
}

export const QuarantineTable: React.FC<QuarantineTableProps> = ({
  items,
  total,
  pagination,
  onPaginationChange,
  onDiscard,
  onReprocess,
  onGetDetail,
  onOpenDetail,
  selection,
}) => {
  const { t } = useTranslation("drift")
  const canDiscard = usePermission("quarantine.discard")

  const [discardTarget, setDiscardTarget] = useState<QuarantineEntry | null>(null)
  const [discardLoading, setDiscardLoading] = useState(false)
  const [discardError, setDiscardError] = useState<string | null>(null)

  const [reprocessTarget, setReprocessTarget] = useState<QuarantineEntry | null>(null)
  const [reprocessLoading, setReprocessLoading] = useState(false)
  const [reprocessLoadingId, setReprocessLoadingId] = useState<string | null>(null)
  const [reprocessNotice, setReprocessNotice] = useState<{
    variant: "success" | "warning" | "danger"
    message: string
  } | null>(null)

  const [successMsg, setSuccessMsg] = useState<string | null>(null)
  const [loadingDetailId, setLoadingDetailId] = useState<string | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)

  const showSuccess = (msg: string) => {
    setSuccessMsg(msg)
    setTimeout(() => setSuccessMsg(null), 3000)
  }

  const showReprocessNotice = (
    variant: "success" | "warning" | "danger",
    message: string,
  ) => {
    setReprocessNotice({ variant, message })
    setTimeout(() => setReprocessNotice(null), 5000)
  }

  const handleDetails = async (entry: QuarantineEntry) => {
    setLoadingDetailId(entry.id)
    setDetailError(null)
    try {
      const detail = await onGetDetail(entry.id)
      onOpenDetail(detail)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : t("quarantine.table.errorUnexpected")
      setDetailError(t("quarantine.table.detailLoadError", { message: msg }))
    } finally {
      setLoadingDetailId(null)
    }
  }

  const handleDiscardConfirm = async () => {
    if (!discardTarget) return
    setDiscardLoading(true)
    setDiscardError(null)
    try {
      await onDiscard(discardTarget.id)
      showSuccess(t("quarantine.table.discardSuccess"))
      setDiscardTarget(null)
    } catch (e: unknown) {
      setDiscardError(e instanceof Error ? e.message : t("quarantine.table.errorUnexpected"))
    } finally {
      setDiscardLoading(false)
    }
  }

  const handleReprocessConfirm = async () => {
    if (!reprocessTarget) return
    setReprocessLoading(true)
    setReprocessLoadingId(reprocessTarget.id)
    const targetId = reprocessTarget.id
    setReprocessTarget(null)
    try {
      await onReprocess(targetId)
      showReprocessNotice("success", t("quarantine.table.reprocessSuccess"))
    } catch (e: unknown) {
      const err = e as { statusCode?: number; message?: string }
      const msg = err?.message ?? t("quarantine.table.errorUnexpected")
      if (err?.statusCode === 409) {
        showReprocessNotice("warning", msg)
      } else if (err?.statusCode === 410) {
        showReprocessNotice("warning", t("quarantine.table.reprocessExpired"))
      } else if (err?.statusCode === 422) {
        showReprocessNotice("danger", msg)
      } else if (err?.statusCode === 403) {
        showReprocessNotice("danger", t("quarantine.table.reprocessForbidden"))
      } else {
        showReprocessNotice("danger", msg)
      }
    } finally {
      setReprocessLoading(false)
      setReprocessLoadingId(null)
    }
  }

  const selectionColumn: TableColumn<AnyRow> | null = selection
    ? {
        key: "__bulk_select__",
        title: (
          <Checkbox
            size="sm"
            aria-label={t("quarantine.table.columns.select")}
            checked={selection.headerCheckboxState === "checked"}
            indeterminate={selection.headerCheckboxState === "indeterminate"}
            onChange={() => selection.toggleAllVisible()}
            data-testid="quarantine-bulk-header-checkbox"
          />
        ),
        dataIndex: "id",
        width: 40,
        render: (_v, row) => {
          const r = row as unknown as QuarantineEntry
          return (
            <Checkbox
              size="sm"
              aria-label={t("quarantine.table.columns.selectRow", { id: r.id })}
              checked={selection.isSelected(r.id)}
              onChange={() => selection.toggleOne(r.id)}
              data-testid={`quarantine-bulk-row-${r.id}`}
              onClick={(e) => e.stopPropagation()}
            />
          )
        },
      }
    : null

  const columns: TableColumn<AnyRow>[] = [
    ...(selectionColumn ? [selectionColumn] : []),
    {
      key: "created_at",
      title: t("quarantine.table.columns.createdAt"),
      dataIndex: "created_at",
      sortable: true,
      width: 110,
      render: (_v, row) => {
        const r = row as unknown as QuarantineEntry
        return <span title={r.created_at}>{formatRelativeTime(r.created_at, t)}</span>
      },
    },
    {
      key: "vendor",
      title: t("quarantine.table.columns.vendor"),
      dataIndex: "vendor",
      width: 110,
      render: (_v, row) => {
        const r = row as unknown as QuarantineEntry
        return (
          <span className="block truncate max-w-[110px]" title={r.vendor}>
            {r.vendor}
          </span>
        )
      },
    },
    {
      key: "event_type",
      title: t("quarantine.table.columns.eventType"),
      dataIndex: "event_type",
      width: 150,
      render: (_v, row) => {
        const r = row as unknown as QuarantineEntry
        return (
          <span
            className="block truncate max-w-[150px]"
            title={r.event_type ?? undefined}
          >
            {r.event_type ?? t("quarantine.table.emptyValue")}
          </span>
        )
      },
    },
    {
      key: "error_kind",
      title: t("quarantine.table.columns.errorKind"),
      dataIndex: "error_kind",
      width: 160,
      render: (_v, row) => {
        const r = row as unknown as QuarantineEntry
        const variant = ERROR_KIND_VARIANT[r.error_kind] ?? "default"
        return <Badge variant={variant}>{r.error_kind}</Badge>
      },
    },
    {
      key: "error_detail",
      title: t("quarantine.table.columns.errorDetail"),
      dataIndex: "error_detail",
      render: (_v, row) => {
        const r = row as unknown as QuarantineEntry
        return (
          <span title={r.error_detail ?? undefined} className="text-text-secondary text-xs">
            {truncate(r.error_detail, 60, t("quarantine.table.emptyValue"))}
          </span>
        )
      },
    },
    {
      key: "actions",
      title: t("quarantine.table.columns.statusActions"),
      dataIndex: "id",
      width: 300,
      render: (_v, row) => {
        const r = row as unknown as QuarantineEntry
        const isExpired = new Date(r.expires_at) < new Date()
        const isReprocessing = reprocessLoadingId === r.id

        // Estado terminal (reprocessado/expirado) → badge no lugar do botão
        // de reprocesso. Caso contrário, "Pendente" como rótulo de estado.
        const statusBadge = r.reprocessed_at ? (
          <Badge variant="success">{t("quarantine.table.status.reprocessed")}</Badge>
        ) : isExpired ? (
          <Badge variant="default">{t("quarantine.table.status.expired")}</Badge>
        ) : null

        const canReprocess = !r.reprocessed_at && !isExpired && canDiscard

        return (
          <div
            className="flex items-center gap-1"
            data-testid={`quarantine-row-${r.id}`}
          >
            {/* Indicador de estado: pendente quando ainda há ação possível
                ou quando o usuário não tem permissão para reprocessar. */}
            {statusBadge ?? (
              <Badge variant="default">{t("quarantine.table.status.pending")}</Badge>
            )}

            <Button
              variant="ghost"
              size="xs"
              onClick={() => void handleDetails(r)}
              loading={loadingDetailId === r.id}
              leftIcon={<InfoIcon size={12} />}
              aria-label={t("quarantine.table.actions.detailsAriaLabel", { id: r.id })}
            >
              {t("quarantine.table.actions.details")}
            </Button>

            {canReprocess && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setReprocessTarget(r)}
                loading={isReprocessing}
                leftIcon={<RefreshCwIcon size={12} />}
                aria-label={t("quarantine.table.actions.reprocessAriaLabel", { id: r.id })}
                data-testid={`reprocess-button-${r.id}`}
              >
                {t("quarantine.table.actions.reprocess")}
              </Button>
            )}

            {canDiscard && (
              <Button
                variant="danger"
                size="xs"
                onClick={() => setDiscardTarget(r)}
                leftIcon={<Trash2Icon size={12} />}
                aria-label={t("quarantine.table.actions.discardAriaLabel", { id: r.id })}
                data-testid={`discard-button-${r.id}`}
              >
                {t("quarantine.table.actions.discard")}
              </Button>
            )}
          </div>
        )
      },
    },
  ]

  return (
    <div className="flex flex-col gap-3">
      {successMsg && (
        <Notice variant="success" data-testid="reprocess-success-notice">
          {successMsg}
        </Notice>
      )}
      {discardError && (
        <Notice variant="danger" title={t("quarantine.table.errorTitleDiscard")}>
          {discardError}
        </Notice>
      )}
      {detailError && (
        <Notice
          variant="danger"
          title={t("quarantine.table.errorTitleDetail")}
          data-testid="quarantine-detail-error-notice"
        >
          {detailError}
        </Notice>
      )}
      {reprocessNotice && (
        <Notice
          variant={reprocessNotice.variant}
          data-testid={
            reprocessNotice.variant === "success"
              ? "reprocess-success-notice"
              : "reprocess-error-notice"
          }
        >
          {reprocessNotice.message}
        </Notice>
      )}

      <DataTable
        data={items as unknown as AnyRow[]}
        columns={columns}
        pagination={{ ...pagination, total, showTotal: true, showSizeChanger: true }}
        onPaginationChange={onPaginationChange}
        emptyMessage={t("quarantine.table.emptyMessage")}
        serverSide
      />

      {discardTarget && (
        <ConfirmDialog
          open
          title={t("quarantine.table.confirm.discardTitle")}
          description={t("quarantine.table.confirm.discardDescription", {
            vendor: discardTarget.vendor,
            eventTypeSuffix: discardTarget.event_type ? ` · ${discardTarget.event_type}` : "",
          })}
          confirmLabel={t("quarantine.table.confirm.discardLabel")}
          confirmVariant="danger"
          loading={discardLoading}
          onConfirm={handleDiscardConfirm}
          onClose={() => {
            if (!discardLoading) setDiscardTarget(null)
          }}
        />
      )}

      {reprocessTarget && (
        <ConfirmDialog
          open
          title={t("quarantine.table.confirm.reprocessTitle")}
          description={t("quarantine.table.confirm.reprocessDescription")}
          confirmLabel={t("quarantine.table.confirm.reprocessLabel")}
          confirmVariant="primary"
          loading={reprocessLoading}
          onConfirm={handleReprocessConfirm}
          onClose={() => {
            if (!reprocessLoading) setReprocessTarget(null)
          }}
          data-testid="reprocess-confirm-dialog"
        />
      )}
    </div>
  )
}

export default QuarantineTable
