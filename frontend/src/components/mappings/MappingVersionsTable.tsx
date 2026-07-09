/**
 * MappingVersionsTable
 * Tabela de versões de um mapping com rollback e comparação de diff.
 * Sprint 2.
 */

import { useState } from "react"
import type React from "react"
import { useTranslation } from "react-i18next"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Notice } from "@/components/ui/Notice/Notice"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { MappingDiffModal } from "@/components/mappings/MappingDiffModal"
import { useMappingDiff } from "@/hooks/useMappingDiff"
import { usePermission } from "@/hooks/usePermission"
import { rollbackMapping } from "@/services/api"
import { formatDateTime } from "@/lib/intl"
import type { MappingVersion } from "@/types"
import type { TableColumn } from "@/types"

interface MappingVersionsTableProps {
  mappingId: string
  versions: MappingVersion[]
  currentVersionId: string | null
  onRefetch: () => void
}

export const MappingVersionsTable: React.FC<MappingVersionsTableProps> = ({
  mappingId,
  versions,
  currentVersionId,
  onRefetch,
}) => {
  const { t } = useTranslation("mappings")
  const canRollback = usePermission("mapping.rollback")

  // Multi-select para comparar
  const [selectedIds, setSelectedIds] = useState<string[]>([])

  // Rollback
  const [rollbackTarget, setRollbackTarget] = useState<MappingVersion | null>(null)
  const [rollbackMessage, setRollbackMessage] = useState("")
  const [rollbackMessageError, setRollbackMessageError] = useState<string | null>(null)
  const [isRollingBack, setIsRollingBack] = useState(false)
  const [rollbackError, setRollbackError] = useState<string | null>(null)
  const [rollbackSuccess, setRollbackSuccess] = useState<string | null>(null)

  // Diff modal
  const [diffPair, setDiffPair] = useState<[string, string] | null>(null)

  const { diff: fetchedDiff, isLoading: diffLoading } = useMappingDiff(
    mappingId,
    diffPair ? diffPair[0] : null,
    diffPair ? diffPair[1] : null,
  )

  function toggleSelect(id: string) {
    setSelectedIds((prev) =>
      prev.includes(id)
        ? prev.filter((x) => x !== id)
        : prev.length < 2
          ? [...prev, id]
          : [prev[1], id],
    )
  }

  function handleCompareSelected() {
    if (selectedIds.length === 2) {
      setDiffPair([selectedIds[0], selectedIds[1]])
    }
  }

  function handleCompareVersion(version: MappingVersion) {
    if (!currentVersionId || version.id === currentVersionId) return
    setDiffPair([currentVersionId, version.id])
  }

  async function handleRollbackConfirm() {
    if (!rollbackTarget) return
    if (!rollbackMessage.trim() || rollbackMessage.trim().length < 10) {
      setRollbackMessageError(t("versionsTable.rollback.messageTooShort"))
      return
    }
    setRollbackMessageError(null)
    setIsRollingBack(true)
    setRollbackError(null)

    try {
      await rollbackMapping(mappingId, {
        version_id: rollbackTarget.id,
        commit_message: rollbackMessage.trim(),
      })
      setRollbackSuccess(t("versionsTable.rollback.success", { version: rollbackTarget.version_number }))
      setRollbackTarget(null)
      setRollbackMessage("")
      onRefetch()
    } catch (e: unknown) {
      setRollbackError(e instanceof Error ? e.message : t("versionsTable.rollback.error"))
    } finally {
      setIsRollingBack(false)
    }
  }

  const columns: TableColumn<Record<string, unknown>>[] = [
    {
      key: "select",
      title: "",
      dataIndex: "id",
      width: 40,
      render: (_: unknown, row: Record<string, unknown>) => {
        const id = row.id as string
        return (
          <input
            type="checkbox"
            aria-label={t("versionsTable.selectVersionAriaLabel", { version: row.version_number })}
            checked={selectedIds.includes(id)}
            onChange={() => toggleSelect(id)}
            className="h-4 w-4 rounded border-border text-primary-600 focus:ring-primary-500"
          />
        )
      },
    },
    {
      key: "version_number",
      title: t("versionsTable.columns.version"),
      dataIndex: "version_number",
      width: 80,
      render: (value: unknown, row: Record<string, unknown>) => (
        <div className="flex items-center gap-2">
          <span className="font-mono text-sm">v{value as number}</span>
          {row.id === currentVersionId && (
            <Badge variant="primary" size="sm">{t("versionsTable.current")}</Badge>
          )}
        </div>
      ),
    },
    {
      key: "author",
      title: t("versionsTable.columns.author"),
      dataIndex: "author_username",
      render: (value: unknown) => (
        <span className="text-sm text-text-secondary">{(value as string) ?? "—"}</span>
      ),
    },
    {
      key: "commit_message",
      title: t("versionsTable.columns.message"),
      dataIndex: "commit_message",
      render: (value: unknown) => (
        <span className="text-sm max-w-xs truncate block" title={value as string}>
          {(value as string) || "—"}
        </span>
      ),
    },
    {
      key: "created_at",
      title: t("versionsTable.columns.createdAt"),
      dataIndex: "created_at",
      width: 160,
      render: (value: unknown) => (
        <span className="text-sm text-text-secondary">
          {formatDateTime(value as string, { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" })}
        </span>
      ),
    },
    {
      key: "actions",
      title: t("common:fields.actions"),
      dataIndex: "id",
      width: 160,
      render: (_: unknown, row: Record<string, unknown>) => {
        const id = row.id as string
        const isCurrent = id === currentVersionId

        return (
          <div className="flex items-center gap-1">
            {!isCurrent && currentVersionId && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => handleCompareVersion(row as unknown as MappingVersion)}
                type="button"
              >
                {t("versionsTable.compare")}
              </Button>
            )}
            {!isCurrent && canRollback && (
              <Button
                variant="ghost"
                size="xs"
                onClick={() => {
                  setRollbackTarget(row as unknown as MappingVersion)
                  setRollbackMessage("")
                  setRollbackMessageError(null)
                }}
                data-testid={`rollback-${id}`}
                type="button"
              >
                {t("versionsTable.makeCurrent")}
              </Button>
            )}
            {isCurrent && (
              <span className="text-xs text-text-tertiary">—</span>
            )}
          </div>
        )
      },
    },
  ]

  // Converter MappingVersion[] para Record<string,unknown>[] para o DataTable genérico
  const tableData: Record<string, unknown>[] = versions.map((v) => ({
    id: v.id,
    version_number: v.version_number,
    author_username: v.author_user_id ? String(v.author_user_id) : null,
    commit_message: v.commit_message,
    created_at: v.created_at,
  }))

  // Para diff modal: converter MappingVersionDiffResponse para MappingVersionDiff shape
  const diffForModal = fetchedDiff
    ? {
        reordered_only: fetchedDiff.reordered_only,
        added: fetchedDiff.added,
        removed: fetchedDiff.removed,
        modified: fetchedDiff.modified,
      }
    : null

  const diffVersionLabel = diffPair
    ? (() => {
        const vA = versions.find((v) => v.id === diffPair[0])
        const vB = versions.find((v) => v.id === diffPair[1])
        return vA && vB ? `v${vA.version_number} → v${vB.version_number}` : undefined
      })()
    : undefined

  return (
    <div className="flex flex-col gap-4">
      {rollbackSuccess && (
        <Notice variant="success">{rollbackSuccess}</Notice>
      )}
      {rollbackError && (
        <Notice variant="danger" title={t("versionsTable.rollback.errorTitle")}>{rollbackError}</Notice>
      )}

      {selectedIds.length === 2 && (
        <div className="flex items-center gap-3">
          <span className="text-sm text-text-secondary">
            {t("versionsTable.selectedCount", { count: selectedIds.length })}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={handleCompareSelected}
            type="button"
          >
            {t("versionsTable.compareSelected")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setSelectedIds([])}
            type="button"
          >
            {t("versionsTable.clearSelection")}
          </Button>
        </div>
      )}

      <DataTable
        data={tableData}
        columns={columns}
        emptyMessage={t("versionsTable.emptyMessage")}
      />

      {/* Rollback confirm dialog */}
      <ConfirmDialog
        open={rollbackTarget !== null}
        title={t("versionsTable.rollback.confirmTitle", { version: rollbackTarget?.version_number })}
        description={
          <div className="flex flex-col gap-3">
            <p>
              {t("versionsTable.rollback.confirmDescription", { version: rollbackTarget?.version_number })}
            </p>
            <Textarea
              label={t("versionsTable.rollback.commitMessageLabel")}
              required
              rows={2}
              placeholder={t("versionsTable.rollback.commitMessagePlaceholder")}
              value={rollbackMessage}
              onChange={(e) => {
                setRollbackMessage(e.target.value)
                if (rollbackMessageError) setRollbackMessageError(null)
              }}
              error={rollbackMessageError ?? undefined}
              disabled={isRollingBack}
            />
          </div>
        }
        confirmLabel={t("versionsTable.rollback.confirmLabel")}
        cancelLabel={t("common:actions.cancel")}
        confirmVariant="primary"
        loading={isRollingBack}
        onConfirm={handleRollbackConfirm}
        onClose={() => {
          if (!isRollingBack) {
            setRollbackTarget(null)
            setRollbackMessage("")
            setRollbackMessageError(null)
          }
        }}
      />

      {/* Diff modal */}
      <MappingDiffModal
        open={diffPair !== null}
        onClose={() => setDiffPair(null)}
        diff={diffForModal}
        versionLabel={diffVersionLabel}
        isLoading={diffLoading}
      />
    </div>
  )
}

export default MappingVersionsTable
