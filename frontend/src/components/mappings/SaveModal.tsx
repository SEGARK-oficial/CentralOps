/**
 * SaveModal
 * Modal de salvar nova versão do mapping.
 * Exibe diff client-side, requer commit message >= 10 chars.
 * Sprint 2.
 */

import { useMemo, useState } from "react"
import type React from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { Textarea } from "@/components/ui/Textarea/Textarea"
import { Notice } from "@/components/ui/Notice/Notice"
import { MappingDiffModal } from "@/components/mappings/MappingDiffModal"
import { computeDiff } from "@/lib/mappingDiff"
import { createMappingVersion } from "@/services/api"
import type { MappingPayload, MappingRule } from "@/types"

interface SaveModalProps {
  open: boolean
  onClose: () => void
  mappingId: string
  currentRules: MappingRule[]
  draftRules: MappingRule[]
  /** Payload v2 (dict com preprocess+rules) a ser persistido. Obrigatório. */
  draftPayload: MappingPayload
  currentVersionNumber: number
  /** Chamado após salvar com sucesso */
  onSuccess: () => void
  /** Percentual de falha no dry-run (entre 0 e 1) para exibir warning */
  dryRunFailRatio?: number
}

const MIN_COMMIT_MESSAGE_LEN = 10

export const SaveModal: React.FC<SaveModalProps> = ({
  open,
  onClose,
  mappingId,
  currentRules,
  draftRules,
  draftPayload,
  currentVersionNumber,
  onSuccess,
  dryRunFailRatio,
}) => {
  const { t } = useTranslation("mappings")
  const [commitMessage, setCommitMessage] = useState("")
  const [isSaving, setIsSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [commitError, setCommitError] = useState<string | null>(null)
  const [showDiffModal, setShowDiffModal] = useState(false)

  // Memoiza o diff: estável entre keystrokes da textarea, evita
  // recomputar e propagar nova ref para o MappingDiffModal aninhado.
  const diff = useMemo(
    () => computeDiff(currentRules, draftRules),
    [currentRules, draftRules],
  )

  const hasHighFailRate = dryRunFailRatio !== undefined && dryRunFailRatio > 0.5

  function handleClose() {
    if (isSaving) return
    setCommitMessage("")
    setSaveError(null)
    setCommitError(null)
    onClose()
  }

  function validateCommit(msg: string): string | null {
    if (!msg.trim()) return t("saveModal.commitRequired")
    if (msg.trim().length < MIN_COMMIT_MESSAGE_LEN)
      return t("saveModal.commitTooShort", { min: MIN_COMMIT_MESSAGE_LEN })
    return null
  }

  async function handleSave() {
    const err = validateCommit(commitMessage)
    if (err) {
      setCommitError(err)
      return
    }
    setCommitError(null)
    setSaveError(null)
    setIsSaving(true)

    try {
      await createMappingVersion(mappingId, {
        rules: draftPayload,
        commit_message: commitMessage.trim(),
      })
      setCommitMessage("")
      onSuccess()
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : t("saveModal.unknownError")
      setSaveError(msg)
    } finally {
      setIsSaving(false)
    }
  }

  const hasChanges =
    diff.added.length > 0 || diff.removed.length > 0 || diff.modified.length > 0 || diff.reordered_only

  return (
    <>
      <Modal
        open={open}
        onClose={handleClose}
        title={t("saveModal.title")}
        size="lg"
        closeOnOverlayClick={!isSaving}
        closeOnEscape={!isSaving}
      >
        <div data-testid="save-modal" className="flex flex-col gap-6">
          {hasHighFailRate && (
            <Notice variant="warning" title={t("saveModal.highFailRate.title")}>
              {t("saveModal.highFailRate.description")}
            </Notice>
          )}

          {saveError && (
            <Notice variant="danger" title={t("saveModal.saveErrorTitle")}>
              {saveError}
            </Notice>
          )}

          {/* Resumo do diff */}
          <section className="flex flex-col gap-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-text">
                {t("saveModal.diffSummary", { version: currentVersionNumber })}
              </h3>
              {hasChanges && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => setShowDiffModal(true)}
                  type="button"
                >
                  {t("saveModal.viewFullDiff")}
                </Button>
              )}
            </div>

            {!hasChanges ? (
              <Notice variant="info">{t("saveModal.noChanges")}</Notice>
            ) : (
              <div className="flex gap-3 text-sm">
                {diff.added.length > 0 && (
                  <span className="text-success-700 font-medium">
                    +{t("saveModal.diffAdded", { count: diff.added.length })}
                  </span>
                )}
                {diff.removed.length > 0 && (
                  <span className="text-danger-700 font-medium">
                    -{t("saveModal.diffRemoved", { count: diff.removed.length })}
                  </span>
                )}
                {diff.modified.length > 0 && (
                  <span className="text-warning-700 font-medium">
                    ~{t("saveModal.diffModified", { count: diff.modified.length })}
                  </span>
                )}
                {diff.reordered_only && (
                  <span className="text-text-secondary font-medium">{t("saveModal.diffReordered")}</span>
                )}
              </div>
            )}
          </section>

          {/* Commit message */}
          <Textarea
            label={t("saveModal.commitMessageLabel")}
            required
            rows={3}
            placeholder={t("saveModal.commitMessagePlaceholder")}
            value={commitMessage}
            onChange={(e) => {
              setCommitMessage(e.target.value)
              if (commitError) setCommitError(null)
            }}
            error={commitError ?? undefined}
            disabled={isSaving}
            data-testid="commit-message-input"
          />

          {/* Footer */}
          <div className="flex justify-end gap-3 border-t border-border pt-4">
            <Button
              variant="outline"
              onClick={handleClose}
              disabled={isSaving}
              type="button"
            >
              {t("common:actions.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={handleSave}
              loading={isSaving}
              data-testid="confirm-save"
              type="button"
            >
              {t("saveModal.confirmSave")}
            </Button>
          </div>
        </div>
      </Modal>

      {/* Diff modal aninhado — só monta quando aberto, evita
          re-render em cada keystroke da textarea de commit. */}
      {showDiffModal && (
        <MappingDiffModal
          open={showDiffModal}
          onClose={() => setShowDiffModal(false)}
          diff={diff}
          versionLabel={t("saveModal.diffVersionLabel", { version: currentVersionNumber })}
        />
      )}
    </>
  )
}

export default SaveModal
