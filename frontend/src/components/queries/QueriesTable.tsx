"use client"

import type React from "react"
import { useTranslation } from "react-i18next"
import { EditIcon, FileTextIcon, TrashIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import type { Query, TableColumn } from "@/types"

interface QueriesTableProps {
  queries: Query[]
  loading?: boolean
  onEdit: (query: Query) => void
  onDelete: (queryId: number) => void
}

// R4-8.6: migrado do par tabela+cartões escrito à mão pro `DataTable`
// (`columns` + `renderMobileCard`) — o ganho é dedup: antes os DOIS layouts
// (desktop/mobile) ficavam sempre no DOM, alternados só por CSS (`md:block`/
// `md:hidden`), e cada campo tinha que ser mantido em sincronia nos dois
// lugares. Nenhuma coluna é `sortable`: a ordenação embutida troca o título
// por um `<button>` (título + ícone ↕) e mudaria o texto acessível das
// colunas sem ganho real aqui.
export const QueriesTable: React.FC<QueriesTableProps> = ({ queries, loading = false, onEdit, onDelete }) => {
  const { t } = useTranslation("queries")

  if (loading) {
    return (
      <div className="flex min-h-[240px] items-center justify-center">
        <LoadingSpinner size="lg" text={t("table.loading")} />
      </div>
    )
  }

  if (queries.length === 0) {
    return (
      <EmptyState
        icon={<FileTextIcon size={48} />}
        title={t("table.emptyTitle")}
        description={t("table.emptyDescription")}
      />
    )
  }

  const columns: TableColumn<Query>[] = [
    {
      key: "query",
      title: t("table.columns.query"),
      dataIndex: "title",
      render: (_value, query) => (
        <div className="max-w-[220px] space-y-1">
          <div className="truncate font-semibold text-text" title={query.title}>
            {query.title}
          </div>
          <div className="text-xs text-text-tertiary">{t("table.idLabel", { id: query.id })}</div>
        </div>
      ),
    },
    {
      key: "description",
      title: t("table.columns.description"),
      dataIndex: "description",
      render: (_value, query) => (
        <span className="line-clamp-2 block max-w-[280px] text-text-secondary" title={query.description || undefined}>
          {query.description || t("table.noDescription")}
        </span>
      ),
    },
    {
      key: "clients",
      title: t("table.columns.clients"),
      dataIndex: "client_ids",
      className: "whitespace-nowrap",
      render: (_value, query) =>
        query.client_ids?.length ? (
          <Badge variant="default" size="sm">
            {t("table.clientsCount", { count: query.client_ids.length })}
          </Badge>
        ) : (
          <Badge variant="outline" size="sm">
            {t("table.noDefaultClients")}
          </Badge>
        ),
    },
    {
      key: "preview",
      title: t("table.columns.preview"),
      dataIndex: "statement",
      render: (_value, query) => (
        <code
          className="block max-w-[320px] truncate rounded bg-surface-tertiary px-2 py-1 text-xs text-text-secondary"
          title={query.statement}
        >
          {query.statement}
        </code>
      ),
    },
    {
      key: "actions",
      title: t("table.columns.actions"),
      dataIndex: "id",
      align: "right",
      render: (_value, query) => (
        <div className="flex justify-end gap-2 whitespace-nowrap">
          <Button size="sm" variant="ghost" onClick={() => onEdit(query)} leftIcon={<EditIcon size={14} />}>
            {t("table.edit")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => onDelete(query.id)} leftIcon={<TrashIcon size={14} />}>
            {t("table.remove")}
          </Button>
        </div>
      ),
    },
  ]

  return (
    <div className="space-y-4">
      <DataTable
        data={queries}
        columns={columns}
        rowKey="id"
        tableAriaLabel={t("table.listAriaLabel")}
        tableClassName="min-w-[760px]"
        renderMobileCard={(query) => (
          <div className="rounded-xl border border-border bg-surface p-4">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate font-semibold text-text" title={query.title}>
                  {query.title}
                </div>
                <div className="text-xs text-text-tertiary">{t("table.idLabel", { id: query.id })}</div>
              </div>
              {query.client_ids?.length ? (
                <Badge variant="default" size="sm">
                  {t("table.clientsCount", { count: query.client_ids.length })}
                </Badge>
              ) : (
                <Badge variant="outline" size="sm">
                  {t("table.noDefaultClients")}
                </Badge>
              )}
            </div>
            <p className="mt-2 line-clamp-2 text-sm text-text-secondary" title={query.description || undefined}>
              {query.description || t("table.noDescription")}
            </p>
            <code className="mt-2 block truncate rounded bg-surface-tertiary px-2 py-1 text-xs text-text-secondary" title={query.statement}>
              {query.statement}
            </code>
            <div className="mt-3 flex gap-2">
              <Button size="sm" variant="ghost" onClick={() => onEdit(query)} leftIcon={<EditIcon size={14} />}>
                {t("table.edit")}
              </Button>
              <Button size="sm" variant="ghost" onClick={() => onDelete(query.id)} leftIcon={<TrashIcon size={14} />}>
                {t("table.remove")}
              </Button>
            </div>
          </div>
        )}
      />

      <div className="flex items-center justify-between rounded-xl border border-border bg-surface-tertiary/50 px-4 py-3 text-sm text-text-secondary">
        <span>{t("table.availableCount")}</span>
        <span className="font-semibold text-text">{queries.length}</span>
      </div>
    </div>
  )
}
