/**
 * QuarantineDetailDrawer
 * Modal grande mostrando todos os detalhes de uma entrada de quarentena:
 * error_detail, raw_payload, metadata e timestamps.
 * Footer: botão Descartar (com gating + ConfirmDialog) + Reprocessar (com gating + ConfirmDialog).
 */

import type React from "react"
import { useEffect, useState } from "react"
import { useTranslation } from "react-i18next"
import { RefreshCwIcon, Trash2Icon, ExternalLinkIcon } from "lucide-react"
import { useNavigate } from "react-router-dom"
import { Modal } from "@/components/ui/Modal/Modal"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Notice } from "@/components/ui/Notice/Notice"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { JsonViewer } from "@/components/shared/JsonViewer"
import { usePermission } from "@/hooks/usePermission"
import { formatDateTime } from "@/lib/intl"
import type { QuarantineDetail, QuarantineEntry } from "@/types"

// ── Helpers ──────────────────────────────────────────────────────────────────

const ERROR_KIND_VARIANT: Record<string, "danger" | "warning" | "default"> = {
  schema_error: "danger",
  missing_required: "danger",
  type_cast_failed: "warning",
  value_map_no_match: "warning",
  jmespath_eval_failed: "warning",
}

function formatDatetime(iso: string): string {
  return formatDateTime(iso, { dateStyle: "medium", timeStyle: "short" })
}

// ── Component ─────────────────────────────────────────────────────────────────

interface QuarantineDetailDrawerProps {
  detail: QuarantineDetail | null
  open: boolean
  onClose: () => void
  onDiscard: (id: string) => Promise<void>
  onReprocess?: (id: string) => Promise<QuarantineEntry>
  /** Mapeamentos existentes para link direto */
  mappings?: Array<{ id: string }>
}

export const QuarantineDetailDrawer: React.FC<QuarantineDetailDrawerProps> = ({
  detail: initialDetail,
  open,
  onClose,
  onDiscard,
  onReprocess,
  mappings = [],
}) => {
  const { t } = useTranslation("quarantine")
  const navigate = useNavigate()
  const canDiscard = usePermission("quarantine.discard")

  const [showDiscardConfirm, setShowDiscardConfirm] = useState(false)
  const [discardLoading, setDiscardLoading] = useState(false)
  const [discardError, setDiscardError] = useState<string | null>(null)

  const [showReprocessConfirm, setShowReprocessConfirm] = useState(false)
  const [reprocessLoading, setReprocessLoading] = useState(false)
  const [reprocessNotice, setReprocessNotice] = useState<{
    variant: "success" | "warning" | "danger"
    message: string
  } | null>(null)

  // Local detail state — updates after successful reprocess
  const [localDetail, setLocalDetail] = useState<QuarantineDetail | null>(null)

  // Reseta o estado transitório (override local + notices/erros) sempre que o
  // item exibido muda ou o drawer reabre, evitando exibir dados/avisos do item
  // anterior ao abrir um novo (ou reabrir o mesmo).
  useEffect(() => {
    setLocalDetail(null)
    setReprocessNotice(null)
    setDiscardError(null)
    setShowDiscardConfirm(false)
    setShowReprocessConfirm(false)
  }, [initialDetail?.id, open])

  const detail = localDetail ?? initialDetail

  if (!detail) return null

  const errorKindVariant = ERROR_KIND_VARIANT[detail.error_kind] ?? "default"
  const mappingExists = detail.mapping_version_id
    ? mappings.some((m) => m.id === detail.mapping_version_id)
    : false

  const isExpired = new Date(detail.expires_at) < new Date()

  const handleDiscardConfirm = async () => {
    setDiscardLoading(true)
    setDiscardError(null)
    try {
      await onDiscard(detail.id)
      setShowDiscardConfirm(false)
      onClose()
    } catch (e: unknown) {
      setDiscardError(e instanceof Error ? e.message : t("errors.unexpected"))
    } finally {
      setDiscardLoading(false)
    }
  }

  const handleReprocessConfirm = async () => {
    if (!onReprocess) return
    setReprocessLoading(true)
    try {
      const updated = await onReprocess(detail.id)
      setShowReprocessConfirm(false)
      // Update local detail with response (reprocessed_at is now set)
      setLocalDetail({ ...detail, ...updated })
      setReprocessNotice({ variant: "success", message: t("notices.reprocessSuccess") })
    } catch (e: unknown) {
      const err = e as { statusCode?: number; message?: string }
      const msg = err?.message ?? t("errors.unexpected")
      setShowReprocessConfirm(false)
      if (err?.statusCode === 409) {
        setReprocessNotice({ variant: "warning", message: msg })
      } else if (err?.statusCode === 410) {
        setReprocessNotice({ variant: "warning", message: t("notices.reprocessExpired") })
      } else if (err?.statusCode === 422) {
        // Entry was updated with new error_kind — reflect in local state if possible
        setReprocessNotice({ variant: "danger", message: msg })
      } else if (err?.statusCode === 403) {
        setReprocessNotice({ variant: "danger", message: t("errors.permissionDenied") })
      } else {
        setReprocessNotice({ variant: "danger", message: msg })
      }
    } finally {
      setReprocessLoading(false)
    }
  }

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        size="xl"
        closeOnEscape
        closeOnOverlayClick
      >
        <div data-testid="quarantine-detail-drawer" className="flex flex-col gap-6">
          {/* Header */}
          <div className="flex items-center gap-3 flex-wrap">
            <span className="font-semibold text-text">
              {detail.vendor}
              {detail.event_type ? ` · ${detail.event_type}` : ""}
            </span>
            <Badge variant={errorKindVariant}>{detail.error_kind}</Badge>
            {detail.reprocessed_at && (
              <Badge variant="success">{t("badges.reprocessed")}</Badge>
            )}
          </div>

          {/* Error detail */}
          {detail.error_detail && (
            <Notice
              variant="danger"
              title={t("detail.errorDetailTitle")}
              role="alert"
            >
              <code className="text-xs break-all">{detail.error_detail}</code>
            </Notice>
          )}

          {/* Reprocess notice */}
          {reprocessNotice && (
            <Notice
              variant={reprocessNotice.variant}
              data-testid={
                reprocessNotice.variant === "success"
                  ? "reprocess-success-notice"
                  : "reprocess-error-notice"
              }
            >
              {reprocessNotice.message}
            </Notice>
          )}

          {/* Raw payload */}
          <section aria-label={t("detail.rawPayload")}>
            <h3 className="text-sm font-semibold text-text mb-2">{t("detail.rawPayload")}</h3>
            <div className="rounded-md border border-border bg-surface-tertiary p-3 overflow-auto max-h-80">
              <JsonViewer data={detail.raw_payload} collapseLevel={3} />
            </div>
          </section>

          {/* Metadata */}
          <section aria-label={t("detail.metadata")}>
            <h3 className="text-sm font-semibold text-text mb-2">{t("detail.metadata")}</h3>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <div>
                <dt className="text-text-secondary">{t("detail.integrationId")}</dt>
                <dd className="font-mono text-text">{detail.integration_id ?? "—"}</dd>
              </div>
              <div>
                <dt className="text-text-secondary">{t("detail.mappingVersion")}</dt>
                <dd className="flex items-center gap-1">
                  <span className="font-mono text-text">{detail.mapping_version_id ?? "—"}</span>
                  {detail.mapping_version_id && mappingExists && (
                    <button
                      type="button"
                      onClick={() => navigate(`/mappings/${detail.mapping_version_id}`)}
                      className="text-primary-600 hover:text-primary-700"
                      aria-label={t("detail.openMappingInEditor")}
                    >
                      <ExternalLinkIcon size={12} />
                    </button>
                  )}
                </dd>
              </div>
            </dl>
          </section>

          {/* Timestamps */}
          <section aria-label={t("detail.timestamps")}>
            <h3 className="text-sm font-semibold text-text mb-2">{t("detail.timestamps")}</h3>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
              <div>
                <dt className="text-text-secondary">{t("detail.createdAt")}</dt>
                <dd className="text-text">{formatDatetime(detail.created_at)}</dd>
              </div>
              <div>
                <dt className="text-text-secondary">{t("detail.expiresAt")}</dt>
                <dd className="text-text">{formatDatetime(detail.expires_at)}</dd>
              </div>
              <div>
                <dt className="text-text-secondary">{t("detail.reprocessedAt")}</dt>
                <dd className="text-text">
                  {detail.reprocessed_at ? formatDatetime(detail.reprocessed_at) : "—"}
                </dd>
              </div>
            </dl>
          </section>

          {discardError && (
            <Notice variant="danger" title={t("errors.discardErrorTitle")}>
              {discardError}
            </Notice>
          )}

          {/* Footer actions */}
          <div className="flex items-center justify-end gap-3 pt-2 border-t border-border">
            {detail.reprocessed_at ? (
              <Badge variant="success">{t("badges.reprocessed")}</Badge>
            ) : isExpired ? (
              <Badge variant="default">{t("badges.expired")}</Badge>
            ) : canDiscard && onReprocess ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setShowReprocessConfirm(true)}
                loading={reprocessLoading}
                leftIcon={<RefreshCwIcon size={14} />}
                aria-label={t("detail.reprocessEventAriaLabel", { id: detail.id })}
                data-testid={`reprocess-button-${detail.id}`}
              >
                {t("actions.reprocess")}
              </Button>
            ) : null}

            {canDiscard && (
              <Button
                variant="danger"
                size="sm"
                onClick={() => setShowDiscardConfirm(true)}
                leftIcon={<Trash2Icon size={14} />}
                aria-label={t("detail.discardEntryAriaLabel")}
                data-testid={`discard-button-${detail.id}`}
              >
                {t("actions.discard")}
              </Button>
            )}

            <Button variant="outline" size="sm" onClick={onClose}>
              {t("common:actions.close")}
            </Button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={showDiscardConfirm}
        title={t("detail.discardConfirmTitle")}
        description={t("detail.discardConfirmDescription", {
          vendor: detail.vendor,
          eventType: detail.event_type ? ` · ${detail.event_type}` : "",
        })}
        confirmLabel={t("actions.discard")}
        confirmVariant="danger"
        loading={discardLoading}
        onConfirm={handleDiscardConfirm}
        onClose={() => {
          if (!discardLoading) setShowDiscardConfirm(false)
        }}
      />

      <ConfirmDialog
        open={showReprocessConfirm}
        title={t("detail.reprocessConfirmTitle")}
        description={t("detail.reprocessConfirmDescription")}
        confirmLabel={t("actions.reprocess")}
        confirmVariant="primary"
        loading={reprocessLoading}
        onConfirm={handleReprocessConfirm}
        onClose={() => {
          if (!reprocessLoading) setShowReprocessConfirm(false)
        }}
        data-testid="reprocess-confirm-dialog"
      />
    </>
  )
}

export default QuarantineDetailDrawer
