/**
 * TemplatePicker
 * Modal de seleção de template OCSF pré-construído.
 *
 * Comportamento:
 * - Lista os templates com name, description e botão "Usar template".
 * - Se o editor já tiver regras, exibe confirmação antes de substituir.
 * - Não mescla — substitui completamente (o usuário pode combinar manualmente).
 * - Acessibilidade: role="dialog", aria-labelledby, focus trap via Modal,
 *   cards são <button> (não div com onClick), Escape fecha.
 */

import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { Modal } from "@/components/ui/Modal/Modal"
import { Button } from "@/components/ui/Button/Button"
import { cn } from "@/lib/utils"
import { OCSF_TEMPLATES } from "@/data/ocsfTemplates"
import type { OcsfTemplate } from "@/data/ocsfTemplates"
import type { MappingRule } from "@/types"

interface TemplatePickerProps {
  open: boolean
  onClose: () => void
  onPick: (template: OcsfTemplate) => void
  /** Número de regras existentes no editor (para o prompt de confirmação). */
  existingRulesCount: number
}

export const TemplatePicker: React.FC<TemplatePickerProps> = ({
  open,
  onClose,
  onPick,
  existingRulesCount,
}) => {
  const { t } = useTranslation("mappings")
  // Template pendente de confirmação quando há regras existentes
  const [pendingTemplate, setPendingTemplate] = useState<OcsfTemplate | null>(null)

  function handlePickAttempt(template: OcsfTemplate) {
    if (existingRulesCount > 0) {
      // Mostra confirmação embutida — não abre outro modal
      setPendingTemplate(template)
    } else {
      // Editor vazio — aplica diretamente
      onPick(template)
      onClose()
    }
  }

  function handleConfirmReplace() {
    if (!pendingTemplate) return
    onPick(pendingTemplate)
    setPendingTemplate(null)
    onClose()
  }

  function handleCancelReplace() {
    setPendingTemplate(null)
  }

  function handleClose() {
    setPendingTemplate(null)
    onClose()
  }

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={t("templatePicker.title")}
      size="md"
      closeOnOverlayClick={!pendingTemplate}
      closeOnEscape={!pendingTemplate}
    >
      <div
        aria-labelledby="template-picker-heading"
        data-testid="template-picker"
        className="flex flex-col gap-4"
      >
        <p id="template-picker-heading" className="sr-only">
          {t("templatePicker.heading")}
        </p>

        {/* Prompt de confirmação — inline, acima dos cards */}
        {pendingTemplate && (
          <div
            role="alertdialog"
            aria-labelledby="template-confirm-title"
            data-testid="template-confirm"
            className="rounded-md border border-warning-300 bg-warning-50 px-3 py-3 flex flex-col gap-2"
          >
            <p
              id="template-confirm-title"
              className="text-sm font-medium text-warning-800"
            >
              {t("templatePicker.confirmReplace.title", { count: existingRulesCount })}
            </p>
            <p className="text-xs text-warning-700">
              {t("templatePicker.confirmReplace.before")} <strong>{pendingTemplate.name}</strong>{" "}
              {t("templatePicker.confirmReplace.after")}
            </p>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="danger"
                size="xs"
                onClick={handleConfirmReplace}
                data-testid="template-confirm-replace"
              >
                {t("templatePicker.confirmReplace.confirm")}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={handleCancelReplace}
                data-testid="template-confirm-cancel"
              >
                {t("common:actions.cancel")}
              </Button>
            </div>
          </div>
        )}

        {/* Lista de templates */}
        <ul
          className="flex flex-col gap-3"
          aria-label={t("templatePicker.listAriaLabel")}
          data-testid="template-list"
        >
          {OCSF_TEMPLATES.map((template) => (
            <li key={template.id}>
              <div
                className={cn(
                  "rounded-lg border border-border bg-surface p-4",
                  "flex items-start justify-between gap-4",
                  pendingTemplate?.id === template.id && "border-warning-400 bg-warning-50/30",
                )}
                data-testid={`template-card-${template.id}`}
              >
                <div className="flex flex-col gap-1 flex-1 min-w-0">
                  <span className="text-sm font-semibold text-text">
                    {template.name}
                  </span>
                  <span className="text-xs text-text-secondary leading-relaxed">
                    {template.description}
                  </span>
                  <span className="text-xs text-text-tertiary mt-1">
                    {t("templatePicker.preconfiguredRules", { count: template.rules.length })}
                  </span>
                </div>
                <button
                  type="button"
                  onClick={() => handlePickAttempt(template)}
                  data-testid={`use-template-${template.id}`}
                  aria-label={t("templatePicker.useTemplateAriaLabel", { name: template.name })}
                  className={cn(
                    "shrink-0 inline-flex items-center justify-center",
                    "h-8 px-3 text-sm font-medium rounded-md",
                    "border border-primary-300 text-primary-700 bg-primary-50",
                    "hover:bg-primary-100 hover:border-primary-400",
                    "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500",
                    "transition-colors",
                  )}
                >
                  {t("templatePicker.useTemplate")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </Modal>
  )
}

export default TemplatePicker
