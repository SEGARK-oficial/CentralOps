"use client"

import { useState, useMemo, useRef } from "react"
import { ChevronLeftIcon, ChevronRightIcon, ChevronsLeftIcon, ChevronsRightIcon } from "lucide-react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { useTranslation } from "react-i18next"
import { Button } from "../Button/Button"
import { Select, type SelectValue } from "../Select/Select"
import { LoadingSpinner } from "../LoadingSpinner/LoadingSpinner"
import { EmptyState } from "../EmptyState/EmptyState"
import { cn } from "@/lib/utils"
import { formatNumber } from "@/lib/intl"
import type { TableColumn, PaginationConfig } from "@/types"

export interface DataTableProps<T = object> {
  data: T[]
  columns: TableColumn<T>[]
  loading?: boolean
  pagination?: PaginationConfig
  onPaginationChange?: (pagination: PaginationConfig) => void
  className?: string
  emptyMessage?: string
  /** Ativa virtualização de linhas via @tanstack/react-virtual (ideal para >500 rows) */
  virtualizeRows?: boolean
  /** Altura do container virtualizado (default: "600px") */
  maxHeight?: number | string
  /**
   * Indica que a paginação é feita pelo servidor: o backend já entrega
   * apenas os itens da página corrente, portanto o DataTable NÃO deve
   * fatiar `data` internamente.
   *
   * Quando `true`, `data` é renderizado diretamente. O componente ainda
   * exibe os controles de paginação (navegação, total de registros) usando
   * os metadados de `pagination`.
   *
   * Backwards-compat: o padrão é `false` — todos os usos existentes sem
   * esta prop continuam fazendo slice client-side.
   */
  serverSide?: boolean
}

export const DataTable = <T extends object>({
  data,
  columns,
  loading = false,
  pagination,
  onPaginationChange,
  className,
  emptyMessage,
  virtualizeRows = false,
  maxHeight = "600px",
  serverSide = false,
}: DataTableProps<T>) => {
  const { t } = useTranslation("ui")
  const [sortColumn, setSortColumn] = useState<string | null>(null)
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc")
  // Ref para o container de scroll quando virtualização está ativa
  const scrollContainerRef = useRef<HTMLDivElement>(null)

  const sortedData = useMemo(() => {
    if (!sortColumn) return data
    return [...data].sort((a, b) => {
      const aValue = (a as Record<string, unknown>)[sortColumn] as string | number | boolean | Date | null | undefined
      const bValue = (b as Record<string, unknown>)[sortColumn] as string | number | boolean | Date | null | undefined
      if (aValue === bValue) return 0
      if (aValue == null) return 1
      if (bValue == null) return -1
      const nA = aValue instanceof Date ? aValue.getTime() : typeof aValue === "string" ? aValue.toLowerCase() : aValue
      const nB = bValue instanceof Date ? bValue.getTime() : typeof bValue === "string" ? bValue.toLowerCase() : bValue
      const cmp = nA < nB ? -1 : 1
      return sortDirection === "asc" ? cmp : -cmp
    })
  }, [data, sortColumn, sortDirection])

  const resolvedTotal = pagination?.total ?? sortedData.length
  const totalPages = pagination ? Math.max(1, Math.ceil(resolvedTotal / pagination.pageSize)) : 1
  const currentPage = pagination ? Math.min(Math.max(pagination.current, 1), totalPages) : 1

  const paginatedData = useMemo(() => {
    // serverSide=true: o backend já entregou apenas os itens desta página,
    // não fazer slice client-side (evita double-pagination).
    if (!pagination || serverSide) return sortedData
    const start = (currentPage - 1) * pagination.pageSize
    return sortedData.slice(start, start + pagination.pageSize)
  }, [currentPage, pagination, serverSide, sortedData])

  // Virtualizer — só instanciado quando virtualizeRows=true; usa paginatedData como fonte
  const rowVirtualizer = useVirtualizer({
    count: virtualizeRows ? paginatedData.length : 0,
    getScrollElement: () => scrollContainerRef.current,
    estimateSize: () => 48, // altura estimada de cada linha em px
    overscan: 5,
  })

  const handleSort = (key: string) => {
    if (sortColumn === key) {
      setSortDirection(sortDirection === "asc" ? "desc" : "asc")
    } else {
      setSortColumn(key)
      setSortDirection("asc")
    }
  }

  const handlePageChange = (page: number) => {
    if (pagination && onPaginationChange) {
      onPaginationChange({ ...pagination, current: Math.min(Math.max(page, 1), totalPages) })
    }
  }

  const handlePageSizeChange = (value: SelectValue) => {
    const pageSize = Array.isArray(value) ? Number.parseInt(String(value[0] || 10), 10) : Number.parseInt(String(value), 10)
    if (pagination && onPaginationChange) {
      onPaginationChange({ ...pagination, pageSize: Number.isNaN(pageSize) ? pagination.pageSize : pageSize, current: 1 })
    }
  }

  const startRecord = pagination ? (resolvedTotal === 0 ? 0 : (currentPage - 1) * pagination.pageSize + 1) : 1
  const endRecord = pagination ? Math.min(currentPage * pagination.pageSize, resolvedTotal) : sortedData.length

  if (loading) {
    return <LoadingSpinner size="lg" text={t("dataTable.loading")} className="py-12" />
  }

  if (data.length === 0) {
    return <EmptyState title={emptyMessage ?? t("dataTable.emptyTitle")} description={t("dataTable.emptyDescription")} />
  }

  // Cabeçalho compartilhado entre os dois modos de renderização
  const tableHead = (
    <thead>
      <tr className="border-b border-border bg-surface-tertiary">
        {columns.map((col) => (
          <th
            key={col.key}
            className={cn(
              "px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary",
              col.className,
            )}
            style={{ width: col.width, textAlign: col.align || "left" }}
            scope="col"
            aria-sort={sortColumn === col.key ? (sortDirection === "asc" ? "ascending" : "descending") : "none"}
          >
            {col.sortable ? (
              // Ordenação operável por teclado: <button> nativo herda foco/Enter/Espaço
              // (WCAG 2.1.1). O aria-sort permanece no <th>.
              // focus-ring: estratégia única de foco do design system.
              <button
                type="button"
                onClick={() => handleSort(col.key)}
                className="-mx-1 inline-flex select-none items-center gap-1 rounded px-1 uppercase tracking-wider transition-colors hover:text-text focus-ring"
              >
                {col.title}
                <span className="text-text-tertiary" aria-hidden="true">
                  {sortColumn === col.key ? (sortDirection === "asc" ? "↑" : "↓") : "↕"}
                </span>
              </button>
            ) : (
              <span className="inline-flex items-center gap-1">{col.title}</span>
            )}
          </th>
        ))}
      </tr>
    </thead>
  )

  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {virtualizeRows ? (
        // Modo virtualizado: container com altura fixa + scroll
        <div
          ref={scrollContainerRef}
          className="overflow-auto rounded-lg border border-border"
          style={{ maxHeight }}
        >
          <table className="w-full text-sm" role="table">
            {tableHead}
            <tbody
              className="divide-y divide-border"
              style={{ height: rowVirtualizer.getTotalSize(), position: "relative" }}
            >
              {rowVirtualizer.getVirtualItems().map((virtualRow) => {
                const record = paginatedData[virtualRow.index]
                return (
                  <tr
                    key={virtualRow.key}
                    data-index={virtualRow.index}
                    ref={rowVirtualizer.measureElement}
                    className="hover:bg-surface-tertiary/50 transition-colors absolute w-full"
                    style={{ transform: `translateY(${virtualRow.start}px)` }}
                  >
                    {columns.map((col) => {
                      const key = col.dataIndex || col.key
                      const val = (record as Record<string, unknown>)[key]
                      return (
                        <td key={col.key} className={cn("px-4 py-3 text-text", col.className)} style={{ textAlign: col.align || "left" }}>
                          {col.render ? col.render(val, record, virtualRow.index) : String(val ?? "")}
                        </td>
                      )
                    })}
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : (
        // Modo padrão: todos os rows no DOM (retrocompatível)
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm" role="table">
            {tableHead}
            <tbody className="divide-y divide-border">
              {paginatedData.map((record, index) => (
                <tr key={index} className="hover:bg-surface-tertiary/50 transition-colors">
                  {columns.map((col) => {
                    const key = col.dataIndex || col.key
                    const val = (record as Record<string, unknown>)[key]
                    return (
                      <td key={col.key} className={cn("px-4 py-3 text-text", col.className)} style={{ textAlign: col.align || "left" }}>
                        {col.render ? col.render(val, record, index) : String(val ?? "")}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {pagination && (
        <div className="flex flex-wrap items-center justify-between gap-4 text-sm">
          <div className="text-text-secondary">
            {pagination.showTotal && (
              <span>
                {t("dataTable.showingRange", {
                  start: startRecord,
                  end: endRecord,
                  total: formatNumber(resolvedTotal),
                })}
              </span>
            )}
          </div>

          <div className="flex items-center gap-4">
            {pagination.showSizeChanger && (
              <div className="flex items-center gap-2">
                <label htmlFor="page-size-select" className="text-text-secondary text-xs">{t("dataTable.itemsPerPage")}</label>
                <Select
                  id="page-size-select"
                  value={pagination.pageSize.toString()}
                  onValueChange={handlePageSizeChange}
                  options={[
                    { value: "10", label: "10" },
                    { value: "20", label: "20" },
                    { value: "50", label: "50" },
                    { value: "100", label: "100" },
                  ]}
                />
              </div>
            )}

            <div className="flex items-center gap-1">
              <Button variant="ghost" size="xs" onClick={() => handlePageChange(1)} disabled={currentPage === 1} aria-label={t("dataTable.firstPage")}>
                <ChevronsLeftIcon size={14} />
              </Button>
              <Button variant="ghost" size="xs" onClick={() => handlePageChange(currentPage - 1)} disabled={currentPage === 1} aria-label={t("dataTable.previousPage")}>
                <ChevronLeftIcon size={14} />
              </Button>
              <span className="px-2 text-text-secondary text-xs">
                {currentPage} / {totalPages}
              </span>
              <Button variant="ghost" size="xs" onClick={() => handlePageChange(currentPage + 1)} disabled={currentPage === totalPages} aria-label={t("dataTable.nextPage")}>
                <ChevronRightIcon size={14} />
              </Button>
              <Button variant="ghost" size="xs" onClick={() => handlePageChange(totalPages)} disabled={currentPage === totalPages} aria-label={t("dataTable.lastPage")}>
                <ChevronsRightIcon size={14} />
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
