/**
 * QuarantinePage — /quarantine
 * Eventos que falharam na normalização.
 *
 * PR #3 (bulk ops): seleção em massa, BulkActionBar, "Selecionar tudo
 * do filtro" (cap 2000), confirmação textual em discard >10 itens.
 */

import { useEffect, useMemo, useState } from "react"
import type React from "react"
import { useTranslation } from "react-i18next"
import {
  PackageXIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Notice } from "@/components/ui/Notice/Notice"
import { Button } from "@/components/ui/Button/Button"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { BulkActionBar } from "@/components/ui/BulkActionBar/BulkActionBar"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Input } from "@/components/ui/Input/Input"
import { QuarantineFiltersBar } from "@/components/normalization/QuarantineFiltersBar"
import { QuarantineSummaryCards } from "@/components/normalization/QuarantineSummaryCards"
import { QuarantineTable } from "@/components/normalization/QuarantineTable"
import { QuarantineDetailDrawer } from "@/components/quarantine/QuarantineDetailDrawer"
import { useQuarantine } from "@/hooks/useQuarantine"
import { useBulkSelection } from "@/hooks/useBulkSelection"
import {
  bulkDiscardQuarantine,
  bulkReprocessQuarantine,
  listQuarantineIds,
  QUARANTINE_BULK_BATCH_SIZE,
  QUARANTINE_BULK_IDS_MAX,
} from "@/services/api"
import { usePermission } from "@/hooks/usePermission"
import type { Mapping, QuarantineDetail, QuarantineEntry } from "@/types"
import type { QuarantineFilters } from "@/hooks/useQuarantine"
import type { PaginationConfig } from "@/types"
import type { SelectOption } from "@/components/ui/Select/Select"

const PAGE_SIZE = 20

// Limiar a partir do qual o discard exige confirmação textual.
const DISCARD_TYPED_CONFIRM_THRESHOLD = 10

/**
 * Pagina IDs em batches alinhados com o cap operacional do backend
 * (500/request) e agrega o resultado bulk para exibição num único notice.
 */
async function discardInBatches(ids: string[]) {
  let processed = 0
  let discarded = 0
  const errors: { id: string; reason: string }[] = []

  for (let i = 0; i < ids.length; i += QUARANTINE_BULK_BATCH_SIZE) {
    const batch = ids.slice(i, i + QUARANTINE_BULK_BATCH_SIZE)
    const res = await bulkDiscardQuarantine(batch)
    processed += res.processed
    discarded += res.discarded
    errors.push(...res.errors)
  }
  return { processed, discarded, errors }
}

async function reprocessInBatches(ids: string[]) {
  let accepted = 0
  let expired = 0
  let already = 0
  const errors: { id: string; reason: string }[] = []

  for (let i = 0; i < ids.length; i += QUARANTINE_BULK_BATCH_SIZE) {
    const batch = ids.slice(i, i + QUARANTINE_BULK_BATCH_SIZE)
    const res = await bulkReprocessQuarantine(batch)
    accepted += res.accepted
    expired += res.expired
    already += res.already_reprocessed
    errors.push(...res.errors)
  }
  return { accepted, expired, already_reprocessed: already, errors }
}

export const QuarantinePage: React.FC = () => {
  const { t } = useTranslation("quarantine")
  const DISCARD_TYPED_CONFIRM_PHRASE = t("bulk.discardTypedConfirmPhrase")

  const [filters, setFilters] = useState<QuarantineFilters>({
    limit: PAGE_SIZE,
    offset: 0,
  })

  const [pagination, setPagination] = useState<PaginationConfig>({
    current: 1,
    pageSize: PAGE_SIZE,
    showTotal: true,
    showSizeChanger: true,
  })

  const [openDetail, setOpenDetail] = useState<QuarantineDetail | null>(null)
  const [mappings, setMappings] = useState<Mapping[]>([])

  useEffect(() => {
    const controller = new AbortController()
    fetch("/api/mappings", {
      credentials: "include",
      signal: controller.signal,
    })
      .then((r) => (r.ok ? r.json() : []))
      .then((data: Mapping[]) => setMappings(Array.isArray(data) ? data : []))
      .catch(() => {/* silently absorb */})
    return () => controller.abort()
  }, [])

  const vendorOptions = useMemo<SelectOption[]>(
    () =>
      [...new Set(mappings.map((m) => m.vendor))].sort().map((v) => ({
        value: v,
        label: v,
      })),
    [mappings],
  )

  const eventTypeOptions = useMemo<SelectOption[]>(
    () =>
      [...new Set(mappings.map((m) => m.event_type))].sort().map((et) => ({
        value: et,
        label: et,
      })),
    [mappings],
  )

  const mappingRefs = useMemo(
    () => mappings.map((m) => ({ id: m.id })),
    [mappings],
  )

  const { items, total, isLoading, error, refetch, discard, reprocess, getDetail } =
    useQuarantine(filters)

  // ── Bulk selection ────────────────────────────────────────────────────
  const canDiscard = usePermission("quarantine.discard")
  const bulk = useBulkSelection<QuarantineEntry>({
    visibleItems: items,
    getId: (e) => e.id,
  })

  // ── Bulk action state ──────────────────────────────────────────────────
  type ActionState =
    | { kind: "idle" }
    | { kind: "loading-select-all" }
    | { kind: "confirm-discard"; ids: string[] }
    | { kind: "confirm-reprocess"; ids: string[] }
    | { kind: "running-discard"; ids: string[] }
    | { kind: "running-reprocess"; ids: string[] }

  const [action, setAction] = useState<ActionState>({ kind: "idle" })
  const [bulkNotice, setBulkNotice] = useState<{
    variant: "success" | "warning" | "danger"
    message: string
  } | null>(null)
  const [typedConfirm, setTypedConfirm] = useState("")
  const [selectAllNotice, setSelectAllNotice] = useState<string | null>(null)

  const flashNotice = (
    variant: "success" | "warning" | "danger",
    message: string,
    ttl = 6000,
  ) => {
    setBulkNotice({ variant, message })
    if (ttl > 0) {
      setTimeout(() => setBulkNotice(null), ttl)
    }
  }

  const handlePaginationChange = (p: PaginationConfig) => {
    setPagination(p)
    setFilters((prev) => ({
      ...prev,
      limit: p.pageSize,
      offset: (p.current - 1) * p.pageSize,
    }))
  }

  const handleFiltersChange = (newFilters: QuarantineFilters) => {
    setFilters({ ...newFilters, limit: pagination.pageSize, offset: 0 })
    setPagination((p) => ({ ...p, current: 1 }))
    bulk.clearSelection()
    setSelectAllNotice(null)
  }

  const handleDiscard = async (id: string) => {
    await discard(id)
    refetch()
  }

  const selectedIds = useMemo(() => Array.from(bulk.selected), [bulk.selected])
  const selectedCount = selectedIds.length

  const handleSelectAllFromFilter = async () => {
    setAction({ kind: "loading-select-all" })
    setSelectAllNotice(null)
    try {
      const res = await listQuarantineIds(
        { ...filters, limit: undefined, offset: undefined },
        QUARANTINE_BULK_IDS_MAX,
      )
      bulk.clearSelection()
      for (const id of res.ids) bulk.toggleOne(id)

      if (res.capped) {
        setSelectAllNotice(
          t("bulk.selectAllCapped", { count: res.ids.length, total: res.total }),
        )
      } else {
        setSelectAllNotice(
          t("bulk.selectAllFromFilter", { count: res.ids.length }),
        )
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("bulk.errors.listIdsFailed")
      flashNotice("danger", msg)
    } finally {
      setAction({ kind: "idle" })
    }
  }

  const requestDiscard = () => {
    setTypedConfirm("")
    setAction({ kind: "confirm-discard", ids: selectedIds })
  }

  const requestReprocess = () => {
    setAction({ kind: "confirm-reprocess", ids: selectedIds })
  }

  const closeAction = () => {
    setTypedConfirm("")
    setAction({ kind: "idle" })
  }

  const runDiscard = async () => {
    if (action.kind !== "confirm-discard") return
    const ids = action.ids
    setAction({ kind: "running-discard", ids })
    try {
      const res = await discardInBatches(ids)
      bulk.clearSelection()
      const failed = res.errors.length
      if (failed === 0) {
        flashNotice(
          "success",
          t("bulk.discardSuccess", { count: res.discarded }),
        )
      } else {
        flashNotice(
          "warning",
          t("bulk.discardPartial", { discarded: res.discarded, failed }),
        )
      }
      refetch()
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("errors.unexpected")
      flashNotice("danger", t("bulk.discardFailed", { message: msg }))
    } finally {
      setAction({ kind: "idle" })
      setTypedConfirm("")
    }
  }

  const runReprocess = async () => {
    if (action.kind !== "confirm-reprocess") return
    const ids = action.ids
    setAction({ kind: "running-reprocess", ids })
    try {
      const res = await reprocessInBatches(ids)
      bulk.clearSelection()
      const parts: string[] = []
      if (res.accepted > 0) parts.push(t("bulk.reprocessParts.accepted", { count: res.accepted }))
      if (res.expired > 0) parts.push(t("bulk.reprocessParts.expired", { count: res.expired }))
      if (res.already_reprocessed > 0)
        parts.push(t("bulk.reprocessParts.alreadyReprocessed", { count: res.already_reprocessed }))
      if (res.errors.length > 0)
        parts.push(t("bulk.reprocessParts.ignored", { count: res.errors.length }))
      flashNotice(
        res.errors.length === 0 ? "success" : "warning",
        t("bulk.reprocessQueued", {
          parts: parts.join(", ") || t("bulk.reprocessParts.nothingToProcess"),
        }),
      )
      refetch()
    } catch (e) {
      const msg = e instanceof Error ? e.message : t("errors.unexpected")
      flashNotice("danger", t("bulk.reprocessFailed", { message: msg }))
    } finally {
      setAction({ kind: "idle" })
    }
  }

  // Mantém o dialog montado tanto na confirmação quanto durante a execução
  // para que `loading` do ConfirmDialog seja visível (spinner + bloqueio).
  const discardDialogOpen =
    action.kind === "confirm-discard" || action.kind === "running-discard"
  const reprocessDialogOpen =
    action.kind === "confirm-reprocess" || action.kind === "running-reprocess"

  const discardCount =
    action.kind === "confirm-discard" || action.kind === "running-discard"
      ? action.ids.length
      : 0
  const discardNeedsTyping =
    action.kind === "confirm-discard" &&
    discardCount > DISCARD_TYPED_CONFIRM_THRESHOLD
  const discardCanProceed =
    action.kind === "confirm-discard" &&
    (!discardNeedsTyping || typedConfirm.trim() === DISCARD_TYPED_CONFIRM_PHRASE)

  const reprocessCount =
    action.kind === "confirm-reprocess" || action.kind === "running-reprocess"
      ? action.ids.length
      : 0

  const isRunning =
    action.kind === "running-discard" || action.kind === "running-reprocess"

  return (
    <div data-testid="quarantine-page" className="flex flex-col gap-6 px-1">
      <PageHeader
        eyebrow={t("page.eyebrow")}
        title={t("page.title")}
        description={t("page.description")}
        icon={<PackageXIcon size={20} />}
      />

      <QuarantineFiltersBar
        filters={filters}
        vendorOptions={vendorOptions}
        eventTypeOptions={eventTypeOptions}
        onFiltersChange={handleFiltersChange}
      />

      <QuarantineSummaryCards
        items={items}
        total={total}
        isLoading={isLoading}
      />

      {bulkNotice && (
        <Notice
          variant={bulkNotice.variant}
          data-testid="quarantine-bulk-notice"
        >
          {bulkNotice.message}
        </Notice>
      )}

      {error && (
        <Notice
          variant="danger"
          title={t("page.loadError")}
          action={
            <Button variant="ghost" size="sm" onClick={refetch}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {error.message}
        </Notice>
      )}

      {canDiscard && total > 0 && !isLoading && (
        <div
          className="flex flex-wrap items-center gap-2"
          data-testid="quarantine-bulk-controls"
        >
          <Button
            variant="ghost"
            size="sm"
            onClick={handleSelectAllFromFilter}
            loading={action.kind === "loading-select-all"}
            disabled={isRunning}
            data-testid="quarantine-select-all-filter"
            aria-label={t("bulk.selectAllFilterAriaLabel")}
          >
            {t("bulk.selectAllFilterButton")}
          </Button>
          {selectAllNotice && (
            <span
              className="text-sm text-text-secondary"
              data-testid="quarantine-select-all-notice"
            >
              {selectAllNotice}
            </span>
          )}
        </div>
      )}

      {selectedCount > 0 && (
        <BulkActionBar
          count={selectedCount}
          onClear={() => {
            bulk.clearSelection()
            setSelectAllNotice(null)
          }}
          contextLabel={t("bulk.contextLabel")}
          data-testid="quarantine-bulk-action-bar"
        >
          <Button
            variant="primary"
            size="sm"
            onClick={requestReprocess}
            disabled={isRunning}
            leftIcon={<RefreshCwIcon size={14} />}
            data-testid="quarantine-bulk-reprocess-btn"
          >
            {t("bulk.reprocessSelected")}
          </Button>
          <Button
            variant="danger"
            size="sm"
            onClick={requestDiscard}
            disabled={isRunning}
            leftIcon={<Trash2Icon size={14} />}
            data-testid="quarantine-bulk-discard-btn"
          >
            {t("bulk.discardSelected")}
          </Button>
        </BulkActionBar>
      )}

      {isLoading && !error && (
        <div className="flex justify-center py-8">
          <LoadingSpinner size="lg" text={t("page.loading")} />
        </div>
      )}

      {!isLoading && !error && (
        <div data-testid="quarantine-table">
          <QuarantineTable
            items={items}
            total={total}
            pagination={{ ...pagination, total }}
            onPaginationChange={handlePaginationChange}
            onDiscard={handleDiscard}
            onReprocess={reprocess}
            onGetDetail={getDetail}
            onOpenDetail={setOpenDetail}
            selection={
              canDiscard
                ? {
                    isSelected: bulk.isSelected,
                    toggleOne: bulk.toggleOne,
                    toggleAllVisible: bulk.toggleAllVisible,
                    headerCheckboxState: bulk.headerCheckboxState,
                  }
                : undefined
            }
          />
        </div>
      )}

      <QuarantineDetailDrawer
        detail={openDetail}
        open={openDetail !== null}
        onClose={() => setOpenDetail(null)}
        onDiscard={handleDiscard}
        onReprocess={reprocess}
        mappings={mappingRefs}
      />

      {discardDialogOpen && (
        <ConfirmDialog
          open
          title={t("bulk.discardDialog.title", { count: discardCount })}
          description={
            <div className="flex flex-col gap-2">
              <span>{t("bulk.discardDialog.irreversibleWarning")}</span>
              {discardNeedsTyping && (
                <div
                  className="flex flex-col gap-1"
                  data-testid="quarantine-bulk-discard-typed"
                >
                  <span className="text-sm font-medium">
                    {t("bulk.discardDialog.typeToConfirmPrefix")}{" "}
                    <code className="rounded bg-surface-tertiary px-1 py-0.5 text-xs">
                      {DISCARD_TYPED_CONFIRM_PHRASE}
                    </code>{" "}
                    {t("bulk.discardDialog.typeToConfirmSuffix")}
                  </span>
                  <Input
                    value={typedConfirm}
                    onChange={(e) => setTypedConfirm(e.target.value)}
                    aria-label={t("bulk.discardDialog.typedConfirmAriaLabel")}
                    autoFocus
                  />
                </div>
              )}
            </div>
          }
          confirmLabel={t("bulk.discardDialog.confirmLabel", { count: discardCount })}
          confirmVariant="danger"
          confirmDisabled={!discardCanProceed}
          loading={action.kind === "running-discard"}
          onConfirm={runDiscard}
          onClose={closeAction}
          data-testid="quarantine-bulk-discard-dialog"
        />
      )}

      {reprocessDialogOpen && (
        <ConfirmDialog
          open
          title={t("bulk.reprocessDialog.title", { count: reprocessCount })}
          description={t("bulk.reprocessDialog.description")}
          confirmLabel={t("actions.reprocess")}
          confirmVariant="primary"
          loading={action.kind === "running-reprocess"}
          onConfirm={runReprocess}
          onClose={closeAction}
          data-testid="quarantine-bulk-reprocess-dialog"
        />
      )}
    </div>
  )
}

export default QuarantinePage
