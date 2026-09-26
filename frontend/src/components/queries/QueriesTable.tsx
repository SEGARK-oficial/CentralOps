"use client"

import type React from "react"
import { useTranslation } from "react-i18next"
import { EditIcon, FileTextIcon, TrashIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import type { Query } from "@/types"

interface QueriesTableProps {
  queries: Query[]
  loading?: boolean
  onEdit: (query: Query) => void
  onDelete: (queryId: number) => void
}

const thCls = "px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary"
const tdCls = "px-4 py-4 align-top text-sm text-text"

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

  return (
    <div className="space-y-4">
      {/* Tablet / desktop: tabela com rolagem horizontal segura. */}
      <div className="hidden overflow-hidden rounded-xl border border-border md:block">
        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm" role="table" aria-label={t("table.listAriaLabel")}>
            <thead className="bg-surface-tertiary">
              <tr className="border-b border-border">
                <th scope="col" className={thCls}>
                  {t("table.columns.query")}
                </th>
                <th scope="col" className={thCls}>
                  {t("table.columns.description")}
                </th>
                <th scope="col" className={`${thCls} whitespace-nowrap`}>
                  {t("table.columns.clients")}
                </th>
                <th scope="col" className={thCls}>
                  {t("table.columns.preview")}
                </th>
                <th scope="col" className={`${thCls} text-right`}>
                  {t("table.columns.actions")}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border bg-surface">
              {queries.map((query) => (
                <tr key={query.id} className="transition-colors hover:bg-surface-tertiary/40">
                  <td className={tdCls}>
                    <div className="max-w-[220px] space-y-1">
                      <div className="truncate font-semibold text-text" title={query.title}>
                        {query.title}
                      </div>
                      <div className="text-xs text-text-tertiary">{t("table.idLabel", { id: query.id })}</div>
                    </div>
                  </td>
                  <td className={tdCls}>
                    <span className="line-clamp-2 block max-w-[280px] text-text-secondary" title={query.description || undefined}>
                      {query.description || t("table.noDescription")}
                    </span>
                  </td>
                  <td className={`${tdCls} whitespace-nowrap`}>
                    {query.client_ids?.length ? (
                      <Badge variant="primary" size="sm">
                        {t("table.clientsCount", { count: query.client_ids.length })}
                      </Badge>
                    ) : (
                      <Badge variant="outline" size="sm">
                        {t("table.noDefaultClients")}
                      </Badge>
                    )}
                  </td>
                  <td className={tdCls}>
                    <code
                      className="block max-w-[320px] truncate rounded bg-surface-tertiary px-2 py-1 text-xs text-text-secondary"
                      title={query.statement}
                    >
                      {query.statement}
                    </code>
                  </td>
                  <td className={tdCls}>
                    <div className="flex justify-end gap-2 whitespace-nowrap">
                      <Button size="sm" variant="ghost" onClick={() => onEdit(query)} leftIcon={<EditIcon size={14} />}>
                        {t("table.edit")}
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => onDelete(query.id)} leftIcon={<TrashIcon size={14} />}>
                        {t("table.remove")}
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Mobile: cada query vira um cartão — sem scroll horizontal. */}
      <div className="space-y-3 md:hidden">
        {queries.map((query) => (
          <div key={query.id} className="rounded-xl border border-border bg-surface p-4">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate font-semibold text-text" title={query.title}>
                  {query.title}
                </div>
                <div className="text-xs text-text-tertiary">{t("table.idLabel", { id: query.id })}</div>
              </div>
              {query.client_ids?.length ? (
                <Badge variant="primary" size="sm">
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
        ))}
      </div>

      <div className="flex items-center justify-between rounded-xl border border-border bg-surface-tertiary/50 px-4 py-3 text-sm text-text-secondary">
        <span>{t("table.availableCount")}</span>
        <span className="font-semibold text-text">{queries.length}</span>
      </div>
    </div>
  )
}
