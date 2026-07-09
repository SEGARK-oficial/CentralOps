/**
 * MappingAuditTable
 * Tabela read-only de auditoria de um mapping com filtros.
 * Sprint 2.
 */

import { useState } from "react"
import type React from "react"
import { useTranslation } from "react-i18next"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { Input } from "@/components/ui/Input/Input"
import { Select } from "@/components/ui/Select/Select"
import { DateRangePicker } from "@/components/ui/DateRangePicker/DateRangePicker"
import { Notice } from "@/components/ui/Notice/Notice"
import { useMappingAudit } from "@/hooks/useMappingAudit"
import { truncateText } from "@/lib/utils"
import { formatDateTime } from "@/lib/intl"
import type { TableColumn } from "@/types"

interface MappingAuditTableProps {
  mappingId: string
}

interface DateRange {
  from: Date | null
  to: Date | null
}

export const MappingAuditTable: React.FC<MappingAuditTableProps> = ({ mappingId }) => {
  const { t } = useTranslation("mappings")
  const ACTION_OPTIONS = [
    { value: "", label: t("auditTable.filters.allActions") },
    { value: "version_created", label: "version_created" },
    { value: "rollback", label: "rollback" },
    { value: "drift_detected", label: "drift_detected" },
    { value: "quarantine", label: "quarantine" },
  ]
  const [actionFilter, setActionFilter] = useState("")
  const [usernameFilter, setUsernameFilter] = useState("")
  const [dateRange, setDateRange] = useState<DateRange>({ from: null, to: null })

  const { entries, isLoading, error } = useMappingAudit(mappingId, {
    action: actionFilter || undefined,
    username: usernameFilter || undefined,
    from_ts: dateRange.from?.toISOString() || undefined,
    to_ts: dateRange.to?.toISOString() || undefined,
    limit: 100,
  })

  const columns: TableColumn<Record<string, unknown>>[] = [
    {
      key: "action",
      title: t("auditTable.columns.action"),
      dataIndex: "action",
      width: 140,
      render: (value: unknown) => (
        <span className="font-mono text-xs bg-surface-tertiary px-1.5 py-0.5 rounded">
          {value as string}
        </span>
      ),
    },
    {
      key: "username",
      title: t("auditTable.columns.username"),
      dataIndex: "username",
      width: 120,
      render: (value: unknown) => (
        <span className="text-sm">{(value as string) ?? "—"}</span>
      ),
    },
    {
      key: "user_role",
      title: t("auditTable.columns.role"),
      dataIndex: "user_role",
      width: 100,
      render: (value: unknown) => (
        <span className="text-sm text-text-secondary">{(value as string) ?? "—"}</span>
      ),
    },
    {
      key: "detail",
      title: t("auditTable.columns.detail"),
      dataIndex: "detail",
      render: (value: unknown) => {
        const text = (value as string) ?? ""
        return (
          <span className="text-sm text-text-secondary" title={text}>
            {truncateText(text, 80, "—")}
          </span>
        )
      },
    },
    {
      key: "created_at",
      title: t("common:fields.date"),
      dataIndex: "created_at",
      width: 160,
      sortable: true,
      render: (value: unknown) => (
        <span className="text-sm text-text-secondary">
          {formatDateTime(value as string, { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })}
        </span>
      ),
    },
  ]

  const tableData: Record<string, unknown>[] = entries.map((e) => ({
    id: e.id,
    action: e.action,
    username: e.username,
    user_role: e.user_role,
    detail: e.detail,
    created_at: e.created_at,
  }))

  return (
    <div className="flex flex-col gap-4">
      {/* Filtros */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="w-48">
          <Select
            label={t("auditTable.filters.actionLabel")}
            options={ACTION_OPTIONS}
            value={actionFilter}
            onValueChange={(v) => setActionFilter(String(v))}
            aria-label={t("auditTable.filters.actionAriaLabel")}
          />
        </div>

        <div className="w-48">
          <Input
            label={t("auditTable.filters.usernameLabel")}
            placeholder={t("auditTable.filters.usernamePlaceholder")}
            value={usernameFilter}
            onChange={(e) => setUsernameFilter(e.target.value)}
          />
        </div>

        <div className="w-72">
          <DateRangePicker
            label={t("auditTable.filters.periodLabel")}
            value={dateRange}
            onChange={(range) => setDateRange(range)}
            aria-label={t("auditTable.filters.periodAriaLabel")}
          />
        </div>
      </div>

      {error && (
        <Notice variant="danger" title={t("auditTable.loadErrorTitle")}>
          {error.message}
        </Notice>
      )}

      <DataTable
        data={tableData}
        columns={columns}
        loading={isLoading}
        emptyMessage={t("auditTable.emptyMessage")}
      />
    </div>
  )
}

export default MappingAuditTable
