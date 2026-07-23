/**
 * DriftExplorerPage — /drift
 * Exibe campos do raw que nenhum mapping consome.
 * Sprint 3: filtros, summary cards (3 chamadas separadas), tabela com ações gated.
 * Fase 4.3: cross-reference de regras por field_path com drawer lateral.
 */

import { useCallback, useEffect, useMemo, useState } from "react"
import type React from "react"
import { useTranslation } from "react-i18next"
import { ActivityIcon, SearchXIcon } from "lucide-react"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Notice } from "@/components/ui/Notice/Notice"
import { Button } from "@/components/ui/Button/Button"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { DriftFiltersBar } from "@/components/normalization/DriftFiltersBar"
import { DriftSummaryCards } from "@/components/normalization/DriftSummaryCards"
import { DriftTable } from "@/components/normalization/DriftTable"
import { DriftBulkActionBar } from "@/components/normalization/DriftBulkActionBar"
import { DriftRulesDrawer } from "@/components/normalization/DriftRulesDrawer"
import { useDrift } from "@/hooks/useDrift"
import { useFieldRules } from "@/hooks/useFieldRules"
import { useBulkSelection } from "@/hooks/useBulkSelection"
import { listDrift, listMappings } from "@/services/api"
import type { MappingListItem } from "@/services/api"
import type { DriftEntry } from "@/types"
import type { DriftFilters } from "@/hooks/useDrift"
import type { MatchedRule } from "@/hooks/useFieldRules"
import type { PaginationConfig } from "@/types"
import type { SelectOption } from "@/components/ui/Select/Select"

const PAGE_SIZE = 20

export const DriftExplorerPage: React.FC = () => {
  const { t } = useTranslation("drift")
  const [filters, setFilters] = useState<DriftFilters>({
    limit: PAGE_SIZE,
    offset: 0,
  })

  const [pagination, setPagination] = useState<PaginationConfig>({
    current: 1,
    pageSize: PAGE_SIZE,
    showTotal: true,
    showSizeChanger: true,
  })

  // ── Summary counters (3 parallel calls) ─────────────────────────────────────
  const [newCount, setNewCount] = useState(0)
  const [ignoredCount, setIgnoredCount] = useState(0)
  const [mappedCount, setMappedCount] = useState(0)
  const [summaryLoading, setSummaryLoading] = useState(true)
  // Antes: um 500 aqui era engolido em silêncio e os cards ficavam em zero,
  // indistinguível de "não há drift" — que é exatamente a leitura errada que o
  // operador faz quando desconfia do detector.
  const [summaryError, setSummaryError] = useState(false)

  const fetchSummaryCounts = useCallback(async () => {
    setSummaryLoading(true)
    try {
      const [newRes, ignoredRes, mappedRes] = await Promise.all([
        listDrift({ status: "new", limit: 1, offset: 0 }),
        listDrift({ status: "ignored", limit: 1, offset: 0 }),
        listDrift({ status: "mapped", limit: 1, offset: 0 }),
      ])
      setNewCount(newRes.total)
      setIgnoredCount(ignoredRes.total)
      setMappedCount(mappedRes.total)
      setSummaryError(false)
    } catch {
      setSummaryError(true)
    } finally {
      setSummaryLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchSummaryCounts()
  }, [fetchSummaryCounts])

  // ── Main data ────────────────────────────────────────────────────────────────
  // Declarado ANTES das opções de filtro: elas fazem união com `items`.
  const { items, total, isLoading, error, refetch, ignoreField, markMapped, deleteField, bulkIgnore, bulkMarkMapped } =
    useDrift(filters)

  // ── Vendor / EventType options from mappings ─────────────────────────────────
  const [mappings, setMappings] = useState<MappingListItem[]>([])

  useEffect(() => {
    // Sprint 3 simplification: fetch mappings once and extract unique sets
    const controller = new AbortController()
    listMappings({ signal: controller.signal })
      .then((data) => setMappings(Array.isArray(data) ? data : []))
      .catch(() => {/* silently absorb */})
    return () => controller.abort()
  }, [])

  // Data-driven read: o motor de drift agora registra campos de fontes SEM
  // MappingDefinition (fonte push/syslog nova, janela de aprendizado). Se as
  // opções viessem só de listMappings(), esse drift existiria na tabela mas
  // seria INFILTRÁVEL. Unimos os vendors/event_types dos mappings com os
  // presentes nas linhas de drift já carregadas.
  const vendorOptions = useMemo<SelectOption[]>(
    () =>
      [...new Set([...mappings.map((m) => m.vendor), ...items.map((d) => d.vendor)])]
        .sort()
        .map((v) => ({ value: v, label: v })),
    [mappings, items],
  )

  const eventTypeOptions = useMemo<SelectOption[]>(
    () =>
      [...new Set([...mappings.map((m) => m.event_type), ...items.map((d) => d.event_type)])]
        .sort()
        .map((et) => ({ value: et, label: et })),
    [mappings, items],
  )

  const mappingRefs = useMemo(
    () => mappings.map((m) => ({ id: m.id, vendor: m.vendor, event_type: m.event_type })),
    [mappings],
  )

  // ── Field rules cross-reference index ───────────────────────────────────────
  const { data: fieldRulesIndex } = useFieldRules()

  // ── Drawer de regras ─────────────────────────────────────────────────────────
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [drawerEntry, setDrawerEntry] = useState<DriftEntry | null>(null)
  const [drawerRules, setDrawerRules] = useState<MatchedRule[]>([])

  const handleOpenRulesDrawer = useCallback((entry: DriftEntry, rules: MatchedRule[]) => {
    setDrawerEntry(entry)
    setDrawerRules(rules)
    setDrawerOpen(true)
  }, [])

  const handleCloseDrawer = useCallback(() => {
    setDrawerOpen(false)
  }, [])

  // ── Bulk selection (via primitive hook) ─────────────────────────────────────
  const {
    selected: selection,
    toggleOne,
    toggleAllVisible,
    clearSelection,
    headerCheckboxState,
  } = useBulkSelection<DriftEntry>({
    visibleItems: items,
    getId: (entry) => entry.id,
  })

  const handlePaginationChange = (p: PaginationConfig) => {
    setPagination(p)
    setFilters((prev) => ({
      ...prev,
      limit: p.pageSize,
      offset: (p.current - 1) * p.pageSize,
    }))
  }

  const handleFiltersChange = (newFilters: DriftFilters) => {
    setFilters({ ...newFilters, limit: pagination.pageSize, offset: 0 })
    setPagination((p) => ({ ...p, current: 1 }))
  }

  // Limpa seleção quando os filtros mudam — evita bulk em ids não visíveis
  const filtersKey = JSON.stringify(filters)
  useEffect(() => {
    clearSelection()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey])

  const handleMutationSuccess = () => {
    refetch()
    void fetchSummaryCounts()
  }

  // Há filtro ativo? (status/vendor/event_type) — distingue "sem drift algum"
  // de "filtro não retornou nada".
  const hasActiveFilter = Boolean(filters.status || filters.vendor || filters.event_type)
  const isEmpty = !isLoading && !error && total === 0 && items.length === 0

  const handleClearFilters = () => {
    handleFiltersChange({})
  }

  return (
    <div data-testid="drift-explorer-page" className="flex flex-col gap-6 px-1">
      <PageHeader
        eyebrow={t("explorer.eyebrow")}
        title={t("explorer.title")}
        description={t("explorer.description")}
        icon={<ActivityIcon size={20} />}
      />

      <DriftFiltersBar
        filters={filters}
        vendorOptions={vendorOptions}
        eventTypeOptions={eventTypeOptions}
        onFiltersChange={handleFiltersChange}
      />

      {summaryError && (
        <Notice
          variant="warning"
          title={t("summary.loadError")}
          action={
            <Button variant="ghost" size="sm" onClick={() => void fetchSummaryCounts()}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {t("summary.loadErrorBody")}
        </Notice>
      )}

      <DriftSummaryCards
        newCount={newCount}
        ignoredCount={ignoredCount}
        mappedCount={mappedCount}
        isLoading={summaryLoading}
        hasError={summaryError}
      />

      <DriftBulkActionBar
        selectedIds={Array.from(selection)}
        onClearSelection={clearSelection}
        onBulkIgnore={async (ids) => {
          await bulkIgnore(ids)
        }}
        onBulkMarkMapped={async (ids) => {
          await bulkMarkMapped(ids)
        }}
        onSuccess={handleMutationSuccess}
      />

      {error && (
        <Notice
          variant="danger"
          title={t("explorer.loadError")}
          action={
            <Button variant="ghost" size="sm" onClick={refetch}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {error.message}
        </Notice>
      )}

      {isLoading && !error && (
        <div className="flex justify-center py-8">
          <LoadingSpinner size="lg" text={t("explorer.loading")} />
        </div>
      )}

      {isEmpty && (
        <EmptyState
          icon={<SearchXIcon size={40} />}
          title={t("explorer.emptyTitle")}
          description={
            hasActiveFilter
              ? t("explorer.emptyDescriptionFiltered")
              : t("explorer.emptyDescriptionUnfiltered")
          }
          action={
            hasActiveFilter ? (
              <Button variant="secondary" size="sm" onClick={handleClearFilters}>
                {t("explorer.clearFilters")}
              </Button>
            ) : undefined
          }
        />
      )}

      {!isLoading && !error && !isEmpty && (
        <div data-testid="drift-table">
          <DriftTable
            items={items}
            total={total}
            pagination={{ ...pagination, total }}
            onPaginationChange={handlePaginationChange}
            onIgnore={async (id) => {
              await ignoreField(id)
              handleMutationSuccess()
            }}
            onMarkMapped={async (id) => {
              await markMapped(id)
              handleMutationSuccess()
            }}
            onDelete={async (id) => {
              await deleteField(id)
              handleMutationSuccess()
            }}
            mappings={mappingRefs}
            fieldRulesIndex={fieldRulesIndex}
            onOpenRulesDrawer={handleOpenRulesDrawer}
            selection={selection}
            onToggleOne={toggleOne}
            onToggleAllVisible={toggleAllVisible}
            headerCheckboxState={headerCheckboxState}
          />
        </div>
      )}

      <DriftRulesDrawer
        open={drawerOpen}
        onClose={handleCloseDrawer}
        field_path={drawerEntry?.field_path ?? ""}
        rules={drawerRules}
      />
    </div>
  )
}

export default DriftExplorerPage
