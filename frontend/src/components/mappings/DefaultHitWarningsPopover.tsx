/**
 * DefaultHitWarningsPopover
 * Fase 4.1b — painel inline que lista regras com 100% de fallback para default.
 * Abre em overlay simples sobre o status bar, fecha em Escape ou clique fora.
 * Cada item tem botão "Marcar como intencional" que emite onMarkIntentional(target).
 */

import type React from "react"
import { useEffect, useId, useRef } from "react"
import { XIcon } from "lucide-react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import type { DryRunDefaultHitWarning } from "@/types"
import { Button } from "@/components/ui/Button/Button"

interface DefaultHitWarningsPopoverProps {
  warnings: DryRunDefaultHitWarning[]
  onMarkIntentional: (target: string) => void
  onClose: () => void
  className?: string
}

export const DefaultHitWarningsPopover: React.FC<DefaultHitWarningsPopoverProps> = ({
  warnings,
  onMarkIntentional,
  onClose,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const titleId = useId()
  const panelRef = useRef<HTMLDivElement>(null)

  // Fechar em Escape
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        onClose()
      }
    }
    document.addEventListener("keydown", handleKeyDown)
    return () => document.removeEventListener("keydown", handleKeyDown)
  }, [onClose])

  // Fechar em clique fora
  useEffect(() => {
    function handlePointerDown(e: PointerEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose()
      }
    }
    // Usar pointerdown (não click) para capturar antes do bubble
    document.addEventListener("pointerdown", handlePointerDown)
    return () => document.removeEventListener("pointerdown", handlePointerDown)
  }, [onClose])

  // Foco inicial no painel ao montar
  useEffect(() => {
    panelRef.current?.focus()
  }, [])

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-labelledby={titleId}
      aria-modal="false"
      data-testid="default-hit-warnings-popover"
      tabIndex={-1}
      className={cn(
        "rounded-lg border border-warning-200 bg-surface shadow-lg",
        "p-4 flex flex-col gap-3",
        "focus-visible:outline-none",
        className,
      )}
    >
      {/* Header */}
      <div className="flex items-center gap-2">
        <h3
          id={titleId}
          className="text-sm font-semibold text-warning-700 flex-1"
        >
          {t("defaultHitWarnings.title")}
        </h3>
        <button
          type="button"
          aria-label={t("defaultHitWarnings.closeAriaLabel")}
          onClick={onClose}
          className="text-text-tertiary hover:text-text transition-colors focus-visible:outline-2 focus-visible:outline-primary-500 rounded"
        >
          <XIcon size={14} aria-hidden="true" />
        </button>
      </div>

      {/* Descrição */}
      <p className="text-xs text-text-secondary">
        {t("defaultHitWarnings.description")}
      </p>

      {/* Lista de avisos */}
      {warnings.length === 0 ? (
        <p className="text-xs text-text-tertiary italic">{t("defaultHitWarnings.noWarnings")}</p>
      ) : (
        <ul className="flex flex-col gap-2" role="list">
          {warnings.map((w) => (
            <li
              key={w.target}
              data-testid={`warning-item-${w.target}`}
              className="flex items-center justify-between gap-2 rounded-md border border-warning-100 bg-warning-50 px-3 py-2"
            >
              <div className="flex flex-col gap-0.5 min-w-0">
                <span className="font-mono text-xs text-text font-medium truncate" title={w.target}>
                  {w.target}
                </span>
                <span className="text-xs text-text-tertiary">
                  {t("defaultHitWarnings.hitRatio", { hitCount: w.hit_count, sampleSize: w.sample_size })}
                </span>
              </div>
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => onMarkIntentional(w.target)}
                data-testid={`mark-intentional-${w.target}`}
                className="shrink-0"
              >
                {t("defaultHitWarnings.markIntentional")}
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default DefaultHitWarningsPopover
