"use client"

import type React from "react"
import { useId, useMemo, useRef, useState } from "react"
import { ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ChevronsLeftIcon, ChevronsRightIcon, ChevronUpIcon } from "lucide-react"
import { useVirtualizer } from "@tanstack/react-virtual"
import { useTranslation } from "react-i18next"
import { Button } from "../Button/Button"
import { Select, type SelectValue } from "../Select/Select"
import { EmptyState } from "../EmptyState/EmptyState"
import { cn } from "@/lib/utils"
import { formatNumber } from "@/lib/intl"
import { useMediaQuery } from "@/hooks/useMediaQuery"
import type { TableColumn, PaginationConfig } from "@/types"

const DEFAULT_MOBILE_BREAKPOINT = "(min-width: 768px)"
const SUMMARY_ROW_ESTIMATE_PX = 34
const DETAIL_ROW_ESTIMATE_PX = 200

/**
 * R2-5.3 — configura uma linha de detalhe expansível (accordion) por
 * registro. O `DataTable` cuida da coluna de alternância, do `aria-expanded`
 * e — com `virtualizeRows` — de achatar "linha resumo + linha de detalhe" em
 * itens virtualizados de altura variável (cada `<tr>` vira um item, medido
 * via `measureElement`), em vez de tratar o par como uma unidade de altura
 * fixa.
 */
export interface ExpandableRowConfig<T> {
  isExpanded: (record: T, index: number) => boolean
  onToggle: (record: T, index: number) => void
  /** Conteúdo da linha de detalhe — renderizado dentro de um <td colSpan> só dele. */
  renderDetail: (record: T, index: number) => React.ReactNode
  /** Nome acessível do botão de alternância — já deve vir de `t()` no chamador. */
  toggleLabel: (record: T, index: number) => string
}

interface RowDescriptor<T> {
  key: React.Key
  kind: "summary" | "detail"
  record: T
  index: number
}

export interface DataTableProps<T = object> {
  data: T[]
  columns: TableColumn<T>[]
  loading?: boolean
  pagination?: PaginationConfig
  onPaginationChange?: (pagination: PaginationConfig) => void
  className?: string
  /** Classe aplicada só ao wrapper da `<table>` (ex.: `min-w-[…]`), sem afetar
   *  a barra de paginação abaixo — que antes herdava a mesma largura mínima. */
  tableClassName?: string
  /** Nome acessível da `<table>` (`aria-label`) — recomendado quando a tela
   *  não tem um `<h1>`/`<h2>` associado visualmente à tabela. */
  tableAriaLabel?: string
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
   *
   * Também desliga a ordenação LOCAL (ver `onSortChange`): ordenar apenas os
   * itens da página corrente por cima de uma paginação server-side produz um
   * resultado incoerente (a página 2 nunca fica adjacente, em ordem, da 1).
   */
  serverSide?: boolean
  /**
   * Chave estável por linha. Aceita o nome de um campo do registro ou uma
   * função `(record, index) => chave`. Sem isto o `key` do React cai no
   * índice da linha, que "escorrega" entre registros vizinhos quando a lista
   * reordena ou remove um item no meio.
   */
  rowKey?: keyof T | ((record: T, index: number) => React.Key)
  /**
   * Notificado quando o usuário pede para ordenar por uma coluna. Use junto
   * de `serverSide` para o pai buscar a página já ordenada no backend — o
   * DataTable nunca ordena localmente uma página parcial.
   */
  onSortChange?: (column: string, direction: "asc" | "desc") => void
  /**
   * R2-5.3 — layout único por breakpoint: abaixo de `mobileBreakpoint`
   * (default `(min-width: 768px)`), renderiza isto por registro em vez da
   * `<table>`. Substitui o padrão antigo de montar tabela E cartões juntos
   * (alternados só por CSS) por um único DOM por vez.
   */
  renderMobileCard?: (record: T, index: number) => React.ReactNode
  /** Media query usada por `renderMobileCard` (default: `(min-width: 768px)`). */
  mobileBreakpoint?: string
  /** Ver `ExpandableRowConfig`. Só se aplica ao layout de tabela (desktop). */
  expandableRow?: ExpandableRowConfig<T>
  /** `data-testid` do wrapper externo (útil pra migrar tabelas escritas à
   *  mão sem quebrar `getByTestId` já existente nos testes). */
  "data-testid"?: string
  /** `data-testid` por linha de dado (não se aplica à linha de detalhe). */
  getRowTestId?: (record: T, index: number) => string | undefined
}

export const DataTable = <T extends object>({
  data,
  columns,
  loading = false,
  pagination,
  onPaginationChange,
  className,
  tableClassName,
  tableAriaLabel,
  emptyMessage,
  virtualizeRows = false,
  maxHeight = "600px",
  serverSide = false,
  rowKey,
  onSortChange,
  renderMobileCard,
  mobileBreakpoint = DEFAULT_MOBILE_BREAKPOINT,
  expandableRow,
  "data-testid": dataTestId,
  getRowTestId,
}: DataTableProps<T>) => {
  const { t } = useTranslation("ui")
  const pageSizeSelectId = useId()
  const [sortColumn, setSortColumn] = useState<string | null>(null)
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc")
  // Ref para o container de scroll quando virtualização está ativa
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  // Só é consultado quando `renderMobileCard` está presente — chamado sempre
  // (regra dos hooks), o valor fica ocioso nos demais casos.
  const isDesktop = useMediaQuery(mobileBreakpoint)

  const getRowKey = (record: T, index: number): React.Key => {
    if (typeof rowKey === "function") return rowKey(record, index)
    if (typeof rowKey === "string" || typeof rowKey === "number") {
      const value = (record as Record<string, unknown>)[rowKey as string]
      if (value != null) return value as React.Key
    }
    return index
  }

  const sortedData = useMemo(() => {
    // serverSide: `data` é só a página corrente. Ordenar client-side aqui
    // ordenaria apenas essa fatia e criaria uma ilusão de ordenação global
    // (a página 2 nunca fica coerente com a 1). Quem quiser ordenar de fato
    // deve reagir a `onSortChange` e buscar a página já ordenada do backend.
    if (!sortColumn || serverSide) return data
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
  }, [data, sortColumn, sortDirection, serverSide])

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

  // R2-5.3: achata "linha resumo + linha de detalhe (se expandida)" numa
  // lista de descritores de <tr> — cada um vira exatamente UM item
  // virtualizável, o que permite altura variável (measureElement) em vez de
  // travar tudo numa altura fixa.
  const rowDescriptors = useMemo<RowDescriptor<T>[]>(() => {
    const out: RowDescriptor<T>[] = []
    paginatedData.forEach((record, index) => {
      const key = getRowKey(record, index)
      out.push({ key, kind: "summary", record, index })
      if (expandableRow?.isExpanded(record, index)) {
        out.push({ key: `${String(key)}__detail`, kind: "detail", record, index })
      }
    })
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paginatedData, expandableRow, rowKey])

  const totalColumnCount = columns.length + (expandableRow ? 1 : 0)

  // Virtualizer — só instanciado quando virtualizeRows=true; usa rowDescriptors como fonte
  const rowVirtualizer = useVirtualizer({
    count: virtualizeRows ? rowDescriptors.length : 0,
    getScrollElement: () => scrollContainerRef.current,
    // Acompanha a densidade da linha (py-2 + text-sm ≈ 34px); detalhe expandido
    // parte de uma estimativa maior, corrigida pelo `measureElement` real.
    estimateSize: (index) => (rowDescriptors[index]?.kind === "detail" ? DETAIL_ROW_ESTIMATE_PX : SUMMARY_ROW_ESTIMATE_PX),
    overscan: 8,
  })
  const virtualItems = virtualizeRows ? rowVirtualizer.getVirtualItems() : []
  // Linhas-espaçadoras (padding-top/bottom) em vez de `<tr absolute>`: dentro
  // de uma <table> o posicionamento absoluto tira a linha do fluxo de
  // colunas do navegador e desalinha o header do body. Duas <tr aria-hidden>
  // com altura calculada mantêm o layout de tabela real E o scroll virtual.
  const paddingTop = virtualItems.length > 0 ? virtualItems[0].start : 0
  const paddingBottom =
    virtualItems.length > 0 ? rowVirtualizer.getTotalSize() - virtualItems[virtualItems.length - 1].end : 0

  const handleSort = (col: TableColumn<T>) => {
    // Ordena pelo campo que a linha realmente expõe (`dataIndex`), não pelo
    // `key` da coluna — uma coluna pode ter `key:"size"` e `dataIndex:
    // "approx_bytes"` (rótulo ≠ campo), e ordenar por "size" comparava
    // `undefined` com `undefined` em toda linha.
    const field = col.dataIndex || col.key
    const nextDirection: "asc" | "desc" = sortColumn === field && sortDirection === "asc" ? "desc" : "asc"
    setSortColumn(field)
    setSortDirection(nextDirection)
    onSortChange?.(field, nextDirection)
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

  // Coluna alinhada à direita é número, por convenção. Mono + tabular alinha
  // dígito com dígito, que é o que faz a tabela ler como painel de instrumento.
  const cellClass = (col: TableColumn<T>) =>
    cn("px-3 py-2 text-text", col.align === "right" && "font-mono tabular-nums", col.className)

  // Cabeçalho compartilhado entre os três modos de renderização (dados,
  // vazio-carregando/skeleton, virtualizado).
  const tableHead = (
    <thead className={cn(virtualizeRows && "sticky top-0 z-10")}>
      <tr className="border-b border-border bg-surface-tertiary">
        {expandableRow && <th className="w-10 px-2 py-2" scope="col" aria-label={t("dataTable.expandColumn")} />}
        {columns.map((col) => {
          const field = col.dataIndex || col.key
          return (
            <th
              key={col.key}
              className={cn(
                "px-3 py-2 text-left text-[11px] font-semibold uppercase tracking-[0.08em] text-text-tertiary",
                col.className,
              )}
              style={{ width: col.width, textAlign: col.align || "left" }}
              scope="col"
              // aria-sort só faz sentido numa coluna operável: uma coluna sem
              // `sortable` anunciar "none" sugere ao leitor de tela um
              // controle que não existe.
              aria-sort={
                col.sortable
                  ? sortColumn === field
                    ? sortDirection === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                  : undefined
              }
            >
              {col.sortable ? (
                // Ordenação operável por teclado: <button> nativo herda foco/Enter/Espaço
                // (WCAG 2.1.1). O aria-sort permanece no <th>.
                // focus-ring: estratégia única de foco do design system.
                <button
                  type="button"
                  onClick={() => handleSort(col)}
                  className="-mx-1 inline-flex select-none items-center gap-1 rounded px-1 uppercase tracking-wider transition-colors hover:text-text focus-ring"
                >
                  {col.title}
                  <span className="text-text-tertiary" aria-hidden="true">
                    {sortColumn === field ? (sortDirection === "asc" ? "↑" : "↓") : "↕"}
                  </span>
                </button>
              ) : (
                <span className="inline-flex items-center gap-1">{col.title}</span>
              )}
            </th>
          )
        })}
      </tr>
    </thead>
  )

  const renderDataCells = (record: T, index: number) =>
    columns.map((col) => {
      const key = col.dataIndex || col.key
      const val = (record as Record<string, unknown>)[key]
      return (
        <td key={col.key} className={cellClass(col)} style={{ textAlign: col.align || "left" }}>
          {col.render ? col.render(val, record, index) : String(val ?? "")}
        </td>
      )
    })

  const renderToggleCell = (record: T, index: number) => {
    if (!expandableRow) return null
    const expanded = expandableRow.isExpanded(record, index)
    return (
      <td className="px-2 py-2 text-center">
        <button
          type="button"
          className="rounded p-1 transition-colors hover:bg-surface-tertiary focus-ring"
          aria-expanded={expanded}
          aria-label={expandableRow.toggleLabel(record, index)}
          onClick={() => expandableRow.onToggle(record, index)}
        >
          {expanded ? <ChevronUpIcon size={14} aria-hidden="true" /> : <ChevronDownIcon size={14} aria-hidden="true" />}
        </button>
      </td>
    )
  }

  const renderRow = (descriptor: RowDescriptor<T>, ariaRowIndex: number, measureRef?: (el: HTMLTableRowElement | null) => void, dataIndexAttr?: number) => {
    if (descriptor.kind === "detail") {
      return (
        <tr key={descriptor.key} ref={measureRef} data-index={dataIndexAttr} aria-rowindex={ariaRowIndex}>
          <td colSpan={totalColumnCount} className="border-0 bg-surface-tertiary/30 p-0">
            {expandableRow?.renderDetail(descriptor.record, descriptor.index)}
          </td>
        </tr>
      )
    }
    return (
      <tr
        key={descriptor.key}
        ref={measureRef}
        data-index={dataIndexAttr}
        data-testid={getRowTestId?.(descriptor.record, descriptor.index)}
        aria-rowindex={ariaRowIndex}
        className="hover:bg-surface-tertiary/50 transition-colors"
      >
        {renderToggleCell(descriptor.record, descriptor.index)}
        {renderDataCells(descriptor.record, descriptor.index)}
      </tr>
    )
  }

  if (loading) {
    // Skeleton com a altura de linha prevista em vez do spinner de página
    // inteira: o spinner colapsa a área da tabela a ~40px e, quando os dados
    // chegam, tudo pula de posição (CLS). O skeleton reserva o espaço final.
    // O anúncio para leitor de tela é um `role="status"` sr-only à parte — o
    // mesmo contrato do LoadingSpinner que este skeleton substitui aqui,
    // fora do fluxo visual da tabela (skeleton em si é só decoração).
    const skeletonRowCount = Math.min(pagination?.pageSize ?? 8, 10)
    return (
      <div className={cn("flex flex-col gap-3", className)} data-testid={dataTestId}>
        <div role="status" aria-live="polite" aria-busy="true" className="sr-only">
          {t("loadingSpinner.loading")}
        </div>
        <div className={cn("overflow-x-auto rounded-lg border border-border", tableClassName)}>
          <table className="w-full text-sm" role="table">
            {tableHead}
            <tbody className="divide-y divide-border">
              {Array.from({ length: skeletonRowCount }).map((_, rowIndex) => (
                <tr key={rowIndex}>
                  {expandableRow && <td className="px-2 py-2" />}
                  {columns.map((col) => (
                    <td key={col.key} className={cellClass(col)}>
                      <span className="block h-4 w-full max-w-[160px] animate-pulse rounded bg-surface-tertiary" />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )
  }

  if (data.length === 0) {
    return <EmptyState title={emptyMessage ?? t("dataTable.emptyTitle")} />
  }

  // +1 pela linha de cabeçalho — aria-rowcount/aria-rowindex dão ao leitor de
  // tela a posição real da linha mesmo quando a virtualização só materializa
  // um subconjunto no DOM (sem isto, "linha 3 de 40" vira "linha 3 de 8").
  const ariaRowCount = rowDescriptors.length + 1

  // R2-5.3: layout único por breakpoint — abaixo dele, cartões; nunca os
  // dois juntos no DOM (o padrão antigo montava mobile+desktop simultâneos,
  // alternados só por CSS, e dobrava o trabalho de render).
  const useMobileCards = Boolean(renderMobileCard) && !isDesktop

  return (
    <div className={cn("flex flex-col gap-3", className)} data-testid={dataTestId}>
      {useMobileCards ? (
        <div className="space-y-3">
          {paginatedData.map((record, index) => (
            <div key={getRowKey(record, index)} data-testid={getRowTestId?.(record, index)}>{renderMobileCard?.(record, index)}</div>
          ))}
        </div>
      ) : virtualizeRows ? (
        // Modo virtualizado: container com altura fixa + scroll
        <div
          ref={scrollContainerRef}
          className={cn("overflow-auto rounded-lg border border-border", tableClassName)}
          style={{ maxHeight }}
        >
          <table className="w-full text-sm" role="table" aria-rowcount={ariaRowCount} aria-label={tableAriaLabel}>
            {tableHead}
            <tbody className="divide-y divide-border">
              {paddingTop > 0 && (
                <tr aria-hidden="true" style={{ height: paddingTop }}>
                  <td colSpan={totalColumnCount} className="p-0 border-0" />
                </tr>
              )}
              {virtualItems.map((virtualRow) =>
                renderRow(rowDescriptors[virtualRow.index], virtualRow.index + 2, rowVirtualizer.measureElement, virtualRow.index),
              )}
              {paddingBottom > 0 && (
                <tr aria-hidden="true" style={{ height: paddingBottom }}>
                  <td colSpan={totalColumnCount} className="p-0 border-0" />
                </tr>
              )}
            </tbody>
          </table>
        </div>
      ) : (
        // Modo padrão: todos os rows no DOM (retrocompatível)
        <div className={cn("overflow-x-auto rounded-lg border border-border", tableClassName)}>
          <table className="w-full text-sm" role="table" aria-rowcount={ariaRowCount} aria-label={tableAriaLabel}>
            {tableHead}
            <tbody className="divide-y divide-border">
              {rowDescriptors.map((descriptor, i) => renderRow(descriptor, i + 2))}
            </tbody>
          </table>
        </div>
      )}

      {pagination && (
        <div className="flex flex-wrap items-center justify-between gap-4 text-xs">
          <div className="text-text-tertiary">
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
                <label htmlFor={pageSizeSelectId} className="text-text-tertiary text-xs">{t("dataTable.itemsPerPage")}</label>
                <Select
                  id={pageSizeSelectId}
                  size="sm"
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
              <span className="px-2 font-mono text-xs tabular-nums text-text-secondary">
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
