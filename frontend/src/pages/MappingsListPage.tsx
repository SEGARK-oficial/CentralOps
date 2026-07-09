/**
 * MappingsListPage — /mappings
 * Visão consolidada de todos os mappings cadastrados.
 * Permite filtrar por vendor/event_type, ver contagem de regras e
 * navegar para o editor individual de cada mapping.
 */

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { LayoutTemplateIcon, PencilIcon, SearchXIcon, FilterXIcon } from "lucide-react"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Notice } from "@/components/ui/Notice/Notice"
import { Button } from "@/components/ui/Button/Button"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { Badge } from "@/components/ui/Badge/Badge"
import { listMappings } from "@/services/api"
import type { MappingListItem } from "@/services/api"
import type { PaginationConfig, TableColumn } from "@/types"
import { formatDate, formatDateTime } from "@/lib/intl"

type AnyRow = Record<string, unknown>

const PAGE_SIZE = 15

export const MappingsListPage: React.FC = () => {
  const { t } = useTranslation("mappings")
  const navigate = useNavigate()
  const [items, setItems] = useState<MappingListItem[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<Error | null>(null)
  const [search, setSearch] = useState("")
  const [vendorFilter, setVendorFilter] = useState<string>("")
  const [eventTypeFilter, setEventTypeFilter] = useState<string>("")
  const [currentPage, setCurrentPage] = useState(1)
  // Por padrão mostra só mappings de vendors com integração ATIVA (no escopo do
  // usuário) — os defaults são seedados para todos os vendors, mas só interessa o
  // que o cliente conectou. O toggle revela todos os disponíveis (pré-configurar).
  const [onlyActive, setOnlyActive] = useState(true)
  // Incrementado por "Tentar novamente" para refazer o fetch após erro.
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setIsLoading(true)
    listMappings({ include_rules_count: true, only_active: onlyActive, signal: controller.signal })
      .then((data) => {
        setItems(data)
        setError(null)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === "AbortError") return
        setError(e instanceof Error ? e : new Error(String(e)))
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoading(false)
      })
    return () => controller.abort()
  }, [reloadToken, onlyActive])

  const retry = useCallback(() => {
    setError(null)
    setReloadToken((t) => t + 1)
  }, [])

  const vendorOptions = useMemo(
    () => [
      { value: "", label: t("list.filters.allVendors") },
      ...[...new Set(items.map((m) => m.vendor))].sort().map((v) => ({ value: v, label: v })),
    ],
    [items, t],
  )

  const eventTypeOptions = useMemo(
    () => [
      { value: "", label: t("list.filters.allEventTypes") },
      ...[...new Set(items.map((m) => m.event_type))].sort().map((et) => ({ value: et, label: et })),
    ],
    [items, t],
  )

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return items.filter((m) => {
      if (vendorFilter && m.vendor !== vendorFilter) return false
      if (eventTypeFilter && m.event_type !== eventTypeFilter) return false
      if (q) {
        const inDesc = (m.description ?? "").toLowerCase().includes(q)
        const inVendor = m.vendor.toLowerCase().includes(q)
        const inEt = m.event_type.toLowerCase().includes(q)
        if (!inDesc && !inVendor && !inEt) return false
      }
      return true
    })
  }, [items, search, vendorFilter, eventTypeFilter])

  const hasActiveFilters = search.trim() !== "" || vendorFilter !== "" || eventTypeFilter !== ""

  // Ao mudar busca/filtros, volta para a 1ª página: evita ficar preso numa
  // página que deixou de existir após o recorte do resultado.
  useEffect(() => {
    setCurrentPage(1)
  }, [search, vendorFilter, eventTypeFilter])

  const pagination: PaginationConfig = {
    current: currentPage,
    pageSize: PAGE_SIZE,
    total: filtered.length,
    showTotal: true,
  }

  const resetFilters = () => {
    setSearch("")
    setVendorFilter("")
    setEventTypeFilter("")
  }

  const columns: TableColumn<AnyRow>[] = [
    {
      key: "vendor",
      title: t("list.columns.vendor"),
      dataIndex: "vendor",
      width: 130,
      sortable: true,
    },
    {
      key: "event_type",
      title: t("list.columns.eventType"),
      dataIndex: "event_type",
      width: 180,
    },
    {
      key: "description",
      title: t("common:fields.description"),
      dataIndex: "description",
      render: (_v, row) => {
        const m = row as unknown as MappingListItem
        return (
          <span
            className="block max-w-[28rem] truncate text-text-secondary text-sm"
            title={m.description ?? undefined}
          >
            {m.description ?? "—"}
          </span>
        )
      },
    },
    {
      key: "rules_count",
      title: t("list.columns.rules"),
      dataIndex: "rules_count",
      width: 100,
      align: "center",
      sortable: true,
      render: (_v, row) => {
        const m = row as unknown as MappingListItem
        const count = m.rules_count ?? 0
        if (m.current_version_id == null) {
          return (
            <Badge variant="warning" size="sm" title={t("list.columns.noVersionTitle")}>
              {t("list.columns.noVersion")}
            </Badge>
          )
        }
        return <span className="font-mono text-sm">{count}</span>
      },
    },
    {
      key: "updated_at",
      title: t("common:fields.updatedAt"),
      dataIndex: "updated_at",
      width: 160,
      sortable: true,
      render: (_v, row) => {
        const m = row as unknown as MappingListItem
        return (
          <span className="text-text-tertiary text-xs">
            {formatDate(m.updated_at)}{" "}
            {formatDateTime(m.updated_at, { hour: "2-digit", minute: "2-digit" })}
          </span>
        )
      },
    },
    {
      key: "actions",
      title: t("common:fields.actions"),
      dataIndex: "id",
      width: 130,
      render: (_v, row) => {
        const m = row as unknown as MappingListItem
        return (
          <Button
            variant="ghost"
            size="xs"
            onClick={() => navigate(`/mappings/${m.id}`)}
            leftIcon={<PencilIcon size={12} />}
            data-testid={`edit-mapping-${m.id}`}
            aria-label={t("list.editAriaLabel", { vendor: m.vendor, eventType: m.event_type })}
          >
            {t("common:actions.edit")}
          </Button>
        )
      },
    },
  ]

  return (
    <div data-testid="mappings-list-page" className="flex flex-col gap-6 px-1">
      <PageHeader
        eyebrow={t("list.eyebrow")}
        title={t("list.title")}
        description={t("list.description")}
        icon={<LayoutTemplateIcon size={20} />}
      />

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex-1 min-w-[200px]">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t("list.searchPlaceholder")}
            data-testid="mappings-search"
            aria-label={t("list.searchAriaLabel")}
          />
        </div>
        <Select
          value={vendorFilter}
          onValueChange={(v) => setVendorFilter(String(v))}
          options={vendorOptions}
          data-testid="mappings-filter-vendor"
          aria-label={t("list.filters.vendorAriaLabel")}
        />
        <Select
          value={eventTypeFilter}
          onValueChange={(v) => setEventTypeFilter(String(v))}
          options={eventTypeOptions}
          data-testid="mappings-filter-event-type"
          aria-label={t("list.filters.eventTypeAriaLabel")}
        />
        <Button
          variant="outline"
          size="md"
          onClick={resetFilters}
          disabled={!hasActiveFilters}
          leftIcon={<FilterXIcon size={14} />}
          data-testid="mappings-reset-filters"
          aria-label={t("list.resetFiltersAriaLabel")}
        >
          {t("list.resetFilters")}
        </Button>
        <label className="flex items-center gap-2 whitespace-nowrap text-sm text-text-secondary">
          <input
            type="checkbox"
            checked={!onlyActive}
            onChange={(e) => setOnlyActive(!e.target.checked)}
            data-testid="mappings-show-all"
            className="h-4 w-4 rounded border-border"
          />
          {t("list.showAllVendors")}
        </label>
      </div>

      {error && (
        <Notice
          variant="danger"
          title={t("list.loadError")}
          action={
            <Button
              variant="outline"
              size="sm"
              onClick={retry}
              data-testid="mappings-retry"
            >
              {t("common:actions.retry")}
            </Button>
          }
        >
          {error.message}
        </Notice>
      )}

      {isLoading && !error && (
        <div className="flex justify-center py-8">
          <LoadingSpinner size="lg" text={t("list.loading")} />
        </div>
      )}

      {!isLoading && !error &&
        (filtered.length === 0 && hasActiveFilters ? (
          <EmptyState
            icon={<SearchXIcon size={48} aria-hidden="true" />}
            title={t("list.emptyFiltered.title")}
            description={t("list.emptyFiltered.description")}
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={resetFilters}
                leftIcon={<FilterXIcon size={14} />}
                data-testid="mappings-empty-clear-filters"
              >
                {t("list.emptyFiltered.clearFilters")}
              </Button>
            }
          />
        ) : items.length === 0 && onlyActive ? (
          <EmptyState
            icon={<LayoutTemplateIcon size={48} aria-hidden="true" />}
            title={t("list.emptyNoActive.title")}
            description={t("list.emptyNoActive.description")}
            action={
              <Button
                variant="outline"
                size="sm"
                onClick={() => setOnlyActive(false)}
                data-testid="mappings-empty-show-all"
              >
                {t("list.emptyNoActive.showAll")}
              </Button>
            }
          />
        ) : (
          // Container de scroll horizontal + largura mínima: em telas estreitas
          // (~168px) preserva a leitura das colunas com rolagem em vez de
          // comprimir o conteúdo. O DataTable usa `w-full`, então o `min-w`
          // precisa morar num wrapper aqui na página.
          <div className="overflow-x-auto">
            <div className="min-w-[760px]">
              <DataTable
                data={filtered as unknown as AnyRow[]}
                columns={columns}
                pagination={pagination}
                onPaginationChange={(p) => setCurrentPage(p.current)}
                emptyMessage={
                  items.length === 0
                    ? t("list.emptyNoData")
                    : t("list.emptyFiltered.title")
                }
              />
            </div>
          </div>
        ))}
    </div>
  )
}

export default MappingsListPage
