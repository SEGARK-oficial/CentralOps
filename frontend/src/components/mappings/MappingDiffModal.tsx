/**
 * MappingDiffModal
 * Modal de diff entre duas versões de um mapping.
 * Aceita um diff pré-computado (client-side, para SaveModal)
 * ou um diff do backend (para MappingVersionsTable).
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { Badge } from "@/components/ui/Badge/Badge"
import { JsonViewer } from "@/components/shared/JsonViewer"
import type { MappingVersionDiff } from "@/lib/mappingDiff"
import type { MappingRule } from "@/types"

interface MappingDiffModalProps {
  open: boolean
  onClose: () => void
  diff: MappingVersionDiff | null
  /** Rótulo do header, ex: "v2 → v3" */
  versionLabel?: string
  isLoading?: boolean
}

interface RuleCardProps {
  rule: MappingRule
  variant: "added" | "removed" | "before" | "after"
}

const variantStyles: Record<RuleCardProps["variant"], string> = {
  added: "border-success-300 bg-success-50",
  removed: "border-danger-300 bg-danger-50",
  before: "border-warning-300 bg-warning-50",
  after: "border-primary-300 bg-primary-50",
}

const RuleCard: React.FC<RuleCardProps> = ({ rule, variant }) => (
  <div className={`rounded-md border px-3 py-2 text-xs font-mono ${variantStyles[variant]}`}>
    <JsonViewer data={rule} collapseLevel={1} />
  </div>
)

export const MappingDiffModal: React.FC<MappingDiffModalProps> = ({
  open,
  onClose,
  diff,
  versionLabel,
  isLoading = false,
}) => {
  const { t } = useTranslation("mappings")
  const title = versionLabel
    ? t("diffModal.titleWithVersion", { versionLabel })
    : t("diffModal.title")

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      size="xl"
    >
      <div data-testid="diff-modal" className="flex flex-col gap-6">
        {isLoading && (
          <div className="text-sm text-text-secondary text-center py-8">
            {t("diffModal.loading")}
          </div>
        )}

        {!isLoading && !diff && (
          <Notice variant="info">{t("diffModal.noDiffAvailable")}</Notice>
        )}

        {!isLoading && diff && diff.reordered_only && (
          <Notice variant="info" title={t("diffModal.reorderedOnlyTitle")}>
            {t("diffModal.reorderedOnlyDescription")}
          </Notice>
        )}

        {!isLoading && diff && !diff.reordered_only && (
          <>
            {diff.added.length === 0 && diff.removed.length === 0 && diff.modified.length === 0 && (
              <Notice variant="success" title={t("diffModal.noChangesTitle")}>
                {t("diffModal.noChangesDescription")}
              </Notice>
            )}

            {diff.added.length > 0 && (
              <section className="flex flex-col gap-3">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-text">{t("diffModal.added")}</h3>
                  <Badge variant="success" size="sm">{diff.added.length}</Badge>
                </div>
                <div className="flex flex-col gap-2">
                  {diff.added.map((rule) => (
                    <RuleCard key={rule.target} rule={rule} variant="added" />
                  ))}
                </div>
              </section>
            )}

            {diff.removed.length > 0 && (
              <section className="flex flex-col gap-3">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-text">{t("diffModal.removed")}</h3>
                  <Badge variant="danger" size="sm">{diff.removed.length}</Badge>
                </div>
                <div className="flex flex-col gap-2">
                  {diff.removed.map((rule) => (
                    <RuleCard key={rule.target} rule={rule} variant="removed" />
                  ))}
                </div>
              </section>
            )}

            {diff.modified.length > 0 && (
              <section className="flex flex-col gap-3">
                <div className="flex items-center gap-2">
                  <h3 className="text-sm font-semibold text-text">{t("diffModal.modified")}</h3>
                  <Badge variant="warning" size="sm">{diff.modified.length}</Badge>
                </div>
                <div className="flex flex-col gap-4">
                  {diff.modified.map((mod) => (
                    <div key={mod.target} className="flex flex-col gap-2">
                      <div className="text-xs font-mono font-semibold text-text-secondary">
                        {mod.target}
                      </div>
                      <div className="grid grid-cols-2 gap-2">
                        <div className="flex flex-col gap-1">
                          <span className="text-xs text-text-tertiary font-medium">{t("diffModal.before")}</span>
                          <RuleCard rule={mod.before} variant="before" />
                        </div>
                        <div className="flex flex-col gap-1">
                          <span className="text-xs text-text-tertiary font-medium">{t("diffModal.after")}</span>
                          <RuleCard rule={mod.after} variant="after" />
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>
    </Modal>
  )
}

export default MappingDiffModal
