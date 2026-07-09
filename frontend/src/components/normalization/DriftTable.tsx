/**
 * DriftTable
 * Tabela de entradas de drift com ações inline: ignorar, marcar mapeado,
 * remover e criar regra. Gating por permissão via usePermission.
 */

import type React from "react"
import { useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  EyeOffIcon,
  CheckCircle2Icon,
  Trash2Icon,
  PlusCircleIcon,
  InboxIcon,
} from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Notice } from "@/components/ui/Notice/Notice"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { usePermission } from "@/hooks/usePermission"
import { cn } from "@/lib/utils"
import { formatNumber } from "@/lib/intl"
import type { FieldRulesIndex, MatchedRule } from "@/hooks/useFieldRules"
import type { HeaderCheckboxState } from "@/hooks/useBulkSelection"
import type { DriftEntry, PaginationConfig, TableColumn } from "@/types"

type TFn = ReturnType<typeof useTranslation>["t"]

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatRelativeTime(iso: string, t: TFn): string {
  const diffMs = Date.now() - new Date(iso).getTime()
  const diffS = Math.floor(diffMs / 1000)
  if (diffS < 60) return t("table.relativeTime.now")
  const diffM = Math.floor(diffS / 60)
  if (diffM < 60) return t("table.relativeTime.minutesAgo", { count: diffM })
  const diffH = Math.floor(diffM / 60)
  if (diffH < 24) return t("table.relativeTime.hoursAgo", { count: diffH })
  const diffD = Math.floor(diffH / 24)
  return t("table.relativeTime.daysAgo", { count: diffD })
}

function truncate(str: string | null | undefined, maxLen: number, emptyValue: string): string {
  if (!str) return emptyValue
  return str.length > maxLen ? `${str.slice(0, maxLen)}…` : str
}

function getStatusBadgeMap(
  t: TFn,
): Record<DriftEntry["status"], { label: string; variant: "danger" | "warning" | "success" }> {
  return {
    new: { label: t("table.status.new"), variant: "danger" },
    ignored: { label: t("table.status.ignored"), variant: "warning" },
    mapped: { label: t("table.status.mapped"), variant: "success" },
  }
}

// ── Component ─────────────────────────────────────────────────────────────────

interface DriftTableProps {
  items: DriftEntry[]
  total: number
  pagination: PaginationConfig
  onPaginationChange: (p: PaginationConfig) => void
  onIgnore: (id: string) => Promise<void>
  onMarkMapped: (id: string) => Promise<void>
  onDelete: (id: string) => Promise<void>
  /** Mapeamentos existentes para saber se há mapping para redirecionar */
  mappings: Array<{ id: string; vendor: string; event_type: string }>
  /** Índice de regras por field_path — se null, ainda carregando */
  fieldRulesIndex?: FieldRulesIndex | null
  /** Callback chamado quando usuário clica na contagem de regras de um campo */
  onOpenRulesDrawer?: (entry: DriftEntry, rules: MatchedRule[]) => void
  /** IDs selecionados para bulk actions — gerenciado pelo pai */
  selection?: Set<string>
  /** Toggle individual de um ID — vindo do useBulkSelection do pai. */
  onToggleOne?: (id: string) => void
  /** Toggle do header (liga/desliga todos visíveis) — do useBulkSelection. */
  onToggleAllVisible?: () => void
  /** Estado tri-state do checkbox do header — do useBulkSelection. */
  headerCheckboxState?: HeaderCheckboxState
}

type PendingAction = {
  type: "ignore" | "mark_mapped" | "delete"
  entry: DriftEntry
}

// Cast helpers — DataTable is typed over Record<string,unknown>; we cast safely
type AnyRow = Record<string, unknown>

export const DriftTable: React.FC<DriftTableProps> = ({
  items,
  total,
  pagination,
  onPaginationChange,
  onIgnore,
  onMarkMapped,
  onDelete,
  mappings,
  fieldRulesIndex,
  onOpenRulesDrawer,
  selection = new Set<string>(),
  onToggleOne,
  onToggleAllVisible,
  headerCheckboxState,
}) => {
  const { t } = useTranslation("drift")
  const navigate = useNavigate()
  const canIgnore = usePermission("drift.ignore")
  const canMarkMapped = usePermission("drift.mark_mapped")
  const canDelete = usePermission("drift.delete")

  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [actionLoading, setActionLoading] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [successMsg, setSuccessMsg] = useState<string | null>(null)

  const showSuccess = (msg: string) => {
    setSuccessMsg(msg)
    setTimeout(() => setSuccessMsg(null), 3000)
  }

  const handleConfirm = async () => {
    if (!pendingAction) return
    setActionLoading(true)
    setActionError(null)
    try {
      const { type, entry } = pendingAction
      if (type === "ignore") {
        await onIgnore(entry.id)
        showSuccess(t("table.successIgnored", { field: entry.field_path }))
      } else if (type === "mark_mapped") {
        await onMarkMapped(entry.id)
        showSuccess(t("table.successMarkedMapped", { field: entry.field_path }))
      } else {
        await onDelete(entry.id)
        showSuccess(t("table.successDeleted", { field: entry.field_path }))
      }
      setPendingAction(null)
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : t("table.errorUnexpected"))
    } finally {
      setActionLoading(false)
    }
  }

  const handleCreateRule = (entry: DriftEntry) => {
    const existing = mappings.find(
      (m) => m.vendor === entry.vendor && m.event_type === entry.event_type,
    )
    if (existing) {
      navigate(
        `/mappings/${existing.id}?prefill_path=${encodeURIComponent(entry.field_path)}`,
      )
    } else {
      navigate(
        `/mappings?action=create&vendor=${encodeURIComponent(entry.vendor)}&event_type=${encodeURIComponent(entry.event_type)}&prefill_path=${encodeURIComponent(entry.field_path)}`,
      )
    }
  }

  // Header tri-state — caller pode passar via prop (vindo do useBulkSelection)
  // ou cair em fallback derivado da `selection` recebida.
  const fallbackAll = items.length > 0 && items.every((it) => selection.has(it.id))
  const fallbackSome = items.some((it) => selection.has(it.id))
  const effectiveHeaderState: HeaderCheckboxState =
    headerCheckboxState ??
    (items.length === 0
      ? "unchecked"
      : fallbackAll
        ? "checked"
        : fallbackSome
          ? "indeterminate"
          : "unchecked")

  const handleHeaderToggle = () => {
    if (onToggleAllVisible) {
      onToggleAllVisible()
    }
  }

  const handleRowToggle = (id: string) => {
    if (onToggleOne) onToggleOne(id)
  }

  const columns: TableColumn<AnyRow>[] = [
    {
      key: "select",
      title: (
        <Checkbox
          size="sm"
          aria-label={t("table.columns.select")}
          checked={effectiveHeaderState === "checked"}
          indeterminate={effectiveHeaderState === "indeterminate"}
          onChange={handleHeaderToggle}
          data-testid="drift-select-all"
        />
      ),
      dataIndex: "id",
      width: 40,
      align: "center",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return (
          <Checkbox
            size="sm"
            aria-label={t("table.columns.selectRow", { field: r.field_path })}
            checked={selection.has(r.id)}
            onChange={() => handleRowToggle(r.id)}
            onClick={(e) => e.stopPropagation()}
            data-testid={`drift-select-${r.id}`}
          />
        )
      },
    },
    {
      key: "vendor",
      title: t("table.columns.vendor"),
      dataIndex: "vendor",
      width: 110,
    },
    {
      key: "event_type",
      title: t("table.columns.eventType"),
      dataIndex: "event_type",
      width: 150,
      className: "hidden md:table-cell",
    },
    {
      key: "field_path",
      title: t("table.columns.field"),
      dataIndex: "field_path",
      // Largura máxima para evitar overflow horizontal em paths longos.
      // truncate + title garante legibilidade full via tooltip nativo.
      width: 220,
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return (
          <span
            className="font-mono text-xs block truncate max-w-[200px]"
            title={r.field_path}
          >
            {r.field_path}
          </span>
        )
      },
    },
    {
      key: "mapped_by",
      title: t("table.columns.mappedBy"),
      dataIndex: "field_path",
      width: 120,
      align: "center",
      className: "hidden md:table-cell",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry

        // Ainda carregando
        if (fieldRulesIndex === undefined || fieldRulesIndex === null) {
          return (
            <span className="text-text-tertiary text-xs" aria-label={t("table.loadingRules")}>
              {t("table.loadingRulesShort")}
            </span>
          )
        }

        const matchedRules = fieldRulesIndex.lookup(r.vendor, r.event_type, r.field_path)
        const count = matchedRules.length

        if (count === 0) {
          return (
            <span className="text-text-tertiary text-xs" aria-label={t("table.noRulesConsume")}>
              {t("table.emptyValue")}
            </span>
          )
        }

        const label = t("table.rulesCount", { count })
        return (
          <button
            type="button"
            className="text-xs text-primary-600 hover:text-primary-700 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 rounded"
            aria-label={t("table.rulesCountAriaLabel", { label })}
            data-testid={`rules-count-${r.id}`}
            onClick={() => onOpenRulesDrawer?.(r, matchedRules)}
          >
            {label}
          </button>
        )
      },
    },
    {
      key: "sample_value",
      title: t("table.columns.sampleValue"),
      dataIndex: "sample_value",
      className: "hidden md:table-cell",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return (
          <span title={r.sample_value ?? undefined} className="text-text-secondary text-xs">
            {truncate(r.sample_value, 50, t("table.emptyValue"))}
          </span>
        )
      },
      width: 180,
    },
    {
      key: "sample_type",
      title: t("table.columns.sampleType"),
      dataIndex: "sample_type",
      className: "hidden lg:table-cell",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return (
          <span className="font-mono text-xs text-text-tertiary">{r.sample_type ?? t("table.emptyValue")}</span>
        )
      },
      width: 80,
    },
    {
      key: "occurrence_count",
      title: t("table.columns.occurrenceCount"),
      dataIndex: "occurrence_count",
      sortable: true,
      width: 100,
      align: "right",
      className: "hidden lg:table-cell",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return formatNumber(r.occurrence_count)
      },
    },
    {
      key: "last_seen",
      title: t("table.columns.lastSeen"),
      dataIndex: "last_seen",
      sortable: true,
      width: 110,
      className: "hidden lg:table-cell",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return <span title={r.last_seen}>{formatRelativeTime(r.last_seen, t)}</span>
      },
    },
    {
      key: "status",
      title: t("table.columns.status"),
      dataIndex: "status",
      width: 100,
      className: "hidden md:table-cell",
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        const s = getStatusBadgeMap(t)[r.status]
        return <Badge variant={s.variant}>{s.label}</Badge>
      },
    },
    {
      key: "actions",
      title: t("table.columns.actions"),
      dataIndex: "id",
      width: 200,
      render: (_v, row) => {
        const r = row as unknown as DriftEntry
        return (
          <div
            className="flex items-center gap-1"
            data-testid={`drift-row-${r.field_path}`}
          >
            {canIgnore && r.status !== "ignored" && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setPendingAction({ type: "ignore", entry: r })}
                leftIcon={<EyeOffIcon size={12} />}
                aria-label={t("table.actions.ignoreAriaLabel", { field: r.field_path })}
                data-testid={`ignore-button-${r.id}`}
              >
                <span className="hidden md:inline">{t("table.actions.ignore")}</span>
              </Button>
            )}
            {canMarkMapped && r.status !== "mapped" && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setPendingAction({ type: "mark_mapped", entry: r })}
                leftIcon={<CheckCircle2Icon size={12} />}
                aria-label={t("table.actions.markMappedAriaLabel", { field: r.field_path })}
                data-testid={`mark-mapped-button-${r.id}`}
              >
                <span className="hidden md:inline">{t("table.actions.markMapped")}</span>
              </Button>
            )}
            {canDelete && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setPendingAction({ type: "delete", entry: r })}
                leftIcon={<Trash2Icon size={12} />}
                aria-label={t("table.actions.deleteAriaLabel", { field: r.field_path })}
                data-testid={`delete-button-${r.id}`}
                className="text-danger-600 hover:text-danger-700"
              >
                <span className="hidden md:inline">{t("table.actions.delete")}</span>
              </Button>
            )}
            <Button
              variant="ghost"
              size="xs"
              onClick={() => handleCreateRule(r)}
              leftIcon={<PlusCircleIcon size={12} />}
              aria-label={t("table.actions.createRuleAriaLabel", { field: r.field_path })}
              className={cn(!canIgnore && !canMarkMapped && !canDelete && "ml-0")}
            >
              <span className="hidden md:inline">{t("table.actions.createRule")}</span>
            </Button>
          </div>
        )
      },
    },
  ]

  const confirmMessages: Record<PendingAction["type"], { title: string; description: string }> = {
    ignore: {
      title: t("table.confirm.ignoreTitle"),
      description: t("table.confirm.ignoreDescription", { field: pendingAction?.entry.field_path }),
    },
    mark_mapped: {
      title: t("table.confirm.markMappedTitle"),
      description: t("table.confirm.markMappedDescription", { field: pendingAction?.entry.field_path }),
    },
    delete: {
      title: t("table.confirm.deleteTitle"),
      description: t("table.confirm.deleteDescription", { field: pendingAction?.entry.field_path }),
    },
  }

  return (
    <div className="flex flex-col gap-3">
      {successMsg && (
        <Notice variant="success">{successMsg}</Notice>
      )}
      {actionError && (
        <Notice variant="danger" title={t("table.errorAction")}>
          {actionError}
        </Notice>
      )}

      {items.length === 0 ? (
        <EmptyState
          icon={<InboxIcon size={40} />}
          title={t("table.emptyTitle")}
          description={t("table.emptyDescription")}
        />
      ) : (
        <DataTable
          data={items as unknown as AnyRow[]}
          columns={columns}
          pagination={{ ...pagination, total, showTotal: true, showSizeChanger: true }}
          onPaginationChange={onPaginationChange}
          emptyMessage={t("table.emptyTitle")}
          serverSide
        />
      )}

      {pendingAction && (
        <ConfirmDialog
          open
          title={confirmMessages[pendingAction.type].title}
          description={confirmMessages[pendingAction.type].description}
          confirmLabel={
            pendingAction.type === "delete"
              ? t("table.confirm.deleteLabel")
              : pendingAction.type === "ignore"
                ? t("table.confirm.ignoreLabel")
                : t("table.confirm.markMappedLabel")
          }
          confirmVariant={pendingAction.type === "delete" ? "danger" : "primary"}
          loading={actionLoading}
          onConfirm={handleConfirm}
          onClose={() => {
            if (!actionLoading) setPendingAction(null)
          }}
        />
      )}
    </div>
  )
}

export default DriftTable
