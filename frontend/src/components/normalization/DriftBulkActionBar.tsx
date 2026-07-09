/**
 * DriftBulkActionBar
 * Barra contextual que aparece quando há entradas de drift selecionadas.
 * Oferece ações em massa: ignorar e marcar como mapeado — gateadas por permissão.
 *
 * Refatorado para usar o primitive `<BulkActionBar>` (slot-based) — preserva
 * test-ids e API pública originais para compatibilidade com testes/page.
 */

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { EyeOffIcon, CheckCircle2Icon } from "lucide-react"
import { Button } from "@/components/ui/Button/Button"
import { BulkActionBar } from "@/components/ui/BulkActionBar/BulkActionBar"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Notice } from "@/components/ui/Notice/Notice"
import { usePermission } from "@/hooks/usePermission"

interface DriftBulkActionBarProps {
  selectedIds: string[]
  onClearSelection: () => void
  onBulkIgnore: (ids: string[]) => Promise<void>
  onBulkMarkMapped: (ids: string[]) => Promise<void>
  onSuccess?: () => void
}

type PendingBulk = "ignore" | "mark_mapped" | null

export const DriftBulkActionBar: React.FC<DriftBulkActionBarProps> = ({
  selectedIds,
  onClearSelection,
  onBulkIgnore,
  onBulkMarkMapped,
  onSuccess,
}) => {
  const { t } = useTranslation("drift")
  const canIgnore = usePermission("drift.ignore")
  const canMarkMapped = usePermission("drift.mark_mapped")
  const [pending, setPending] = useState<PendingBulk>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  if (selectedIds.length === 0) return null

  const handleConfirm = async () => {
    if (!pending) return
    setLoading(true)
    setError(null)
    try {
      if (pending === "ignore") {
        await onBulkIgnore(selectedIds)
        setSuccess(t("bulkBar.successIgnored", { count: selectedIds.length }))
      } else {
        await onBulkMarkMapped(selectedIds)
        setSuccess(t("bulkBar.successMarkedMapped", { count: selectedIds.length }))
      }
      onClearSelection()
      onSuccess?.()
      setPending(null)
      setTimeout(() => setSuccess(null), 3000)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : t("bulkBar.errorUnexpected"))
    } finally {
      setLoading(false)
    }
  }

  const count = selectedIds.length
  const confirmCfg =
    pending === "ignore"
      ? {
          title: t("bulkBar.confirmIgnoreTitle", { count }),
          description: t("bulkBar.confirmIgnoreDescription"),
          confirmLabel: t("bulkBar.confirmIgnoreLabel"),
        }
      : {
          title: t("bulkBar.confirmMarkMappedTitle", { count }),
          description: t("bulkBar.confirmMarkMappedDescription"),
          confirmLabel: t("bulkBar.confirmMarkMappedLabel"),
        }

  return (
    <>
      <BulkActionBar
        count={selectedIds.length}
        onClear={onClearSelection}
        data-testid="drift-bulk-bar"
      >
        {canIgnore && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPending("ignore")}
            leftIcon={<EyeOffIcon size={14} />}
            data-testid="drift-bulk-ignore"
          >
            {t("bulkBar.ignore")}
          </Button>
        )}
        {canMarkMapped && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPending("mark_mapped")}
            leftIcon={<CheckCircle2Icon size={14} />}
            data-testid="drift-bulk-mark-mapped"
          >
            {t("bulkBar.markMapped")}
          </Button>
        )}
      </BulkActionBar>
      {success && <Notice variant="success">{success}</Notice>}
      {error && <Notice variant="danger" title={t("bulkBar.errorBulkAction")}>{error}</Notice>}
      {pending && (
        <ConfirmDialog
          open
          title={confirmCfg.title}
          description={confirmCfg.description}
          confirmLabel={confirmCfg.confirmLabel}
          confirmVariant="primary"
          loading={loading}
          onConfirm={handleConfirm}
          onClose={() => {
            if (!loading) setPending(null)
          }}
        />
      )}
    </>
  )
}

export default DriftBulkActionBar
