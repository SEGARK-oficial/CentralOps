"use client"

import type React from "react"
import { AlertTriangleIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { Button } from "@/components/ui/Button/Button"
import { Modal } from "@/components/ui/Modal/Modal"

interface ConfirmDialogProps {
  open: boolean
  title: string
  description: React.ReactNode
  confirmLabel?: string
  cancelLabel?: string
  confirmVariant?: "primary" | "danger"
  loading?: boolean
  /** PR #3: bloqueia o botão de confirmação independentemente de
   *  ``loading``. Útil para fluxos que exigem confirmação textual antes
   *  de habilitar a ação (ex: digitar "DESCARTAR" para discard >10). */
  confirmDisabled?: boolean
  /** PR #3: override de test-id para automação. */
  "data-testid"?: string
  onConfirm: () => void | Promise<void>
  onClose: () => void
}

export const ConfirmDialog: React.FC<ConfirmDialogProps> = ({
  open,
  title,
  description,
  confirmLabel,
  cancelLabel,
  confirmVariant = "danger",
  loading = false,
  confirmDisabled = false,
  "data-testid": dataTestId,
  onConfirm,
  onClose,
}) => {
  const { t } = useTranslation("ui")
  const resolvedConfirmLabel = confirmLabel ?? t("confirmDialog.confirm")
  const resolvedCancelLabel = cancelLabel ?? t("confirmDialog.cancel")
  const handleClose = () => {
    if (!loading) onClose()
  }

  return (
    <Modal open={open} onClose={handleClose} title={title} size="sm" closeOnOverlayClick={!loading} closeOnEscape={!loading}>
      <div className="flex flex-col gap-4" data-testid={dataTestId}>
        <div className="flex items-start gap-3">
          <div className={`shrink-0 p-2 rounded-full ${confirmVariant === "danger" ? "bg-danger-50 text-danger-500" : "bg-primary-50 text-primary-500"}`} aria-hidden="true">
            <AlertTriangleIcon size={20} />
          </div>
          <div className="text-sm text-text-secondary leading-relaxed">{description}</div>
        </div>

        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" variant="outline" onClick={handleClose} disabled={loading}>
            {resolvedCancelLabel}
          </Button>
          <Button
            type="button"
            variant={confirmVariant === "danger" ? "danger" : "primary"}
            onClick={() => void onConfirm()}
            loading={loading}
            disabled={confirmDisabled}
            data-testid={dataTestId ? `${dataTestId}-confirm` : undefined}
          >
            {resolvedConfirmLabel}
          </Button>
        </div>
      </div>
    </Modal>
  )
}

export default ConfirmDialog
