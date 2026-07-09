/**
 * DryRunStatusBar
 * Barra horizontal com badges de resumo do dry-run:
 * amostras (neutro), ok (verde), falhas (vermelho se >0).
 * Quando isPending: LoadingSpinner à direita.
 * Fase 4.1b: chip amarelo de avisos de 100% default quando há warnings.
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { cn } from "@/lib/utils"
import { Badge } from "@/components/ui/Badge/Badge"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import type { DryRunDefaultHitWarning } from "@/types"

interface DryRunStatusBarProps {
  sampleSize: number
  okCount: number
  failCount: number
  isPending: boolean
  /** Fase 4.1b: lista de avisos de 100% default hit. Oculto durante isPending. */
  default_hit_warnings?: DryRunDefaultHitWarning[]
  /** Callback chamado quando o usuário clica no chip de avisos. */
  onWarningsClick?: () => void
  className?: string
}

export const DryRunStatusBar: React.FC<DryRunStatusBarProps> = ({
  sampleSize,
  okCount,
  failCount,
  isPending,
  default_hit_warnings = [],
  onWarningsClick,
  className,
}) => {
  const { t } = useTranslation("mappings")
  const warningCount = default_hit_warnings.length
  const showWarnings = !isPending && warningCount > 0

  return (
    <div
      data-testid="dry-run-status-bar"
      className={cn("flex items-center gap-2 flex-wrap", className)}
      aria-live="polite"
      aria-label={t("dryRunStatusBar.statusAriaLabel")}
      role="status"
    >
      <Badge variant="default" size="sm">
        {t("dryRunStatusBar.samples", { count: sampleSize })}
      </Badge>

      <Badge variant="success" size="sm">
        {t("dryRunStatusBar.ok", { count: okCount })}
      </Badge>

      <Badge variant={failCount > 0 ? "danger" : "default"} size="sm">
        {t("dryRunStatusBar.failures", { count: failCount })}
      </Badge>

      {showWarnings && (
        <button
          type="button"
          data-testid="default-hit-warnings-chip"
          aria-label={t("dryRunStatusBar.warningsChipAriaLabel", { count: warningCount })}
          onClick={onWarningsClick}
          className={cn(
            "inline-flex items-center gap-1 font-medium rounded-full whitespace-nowrap",
            "px-2 py-0.5 text-xs",
            "bg-warning-50 text-warning-700",
            "hover:bg-warning-100 transition-colors",
            "focus-visible:outline-2 focus-visible:outline-warning-500 rounded-full",
          )}
        >
          ⚠ {t("dryRunStatusBar.warningsChipLabel", { count: warningCount })}
        </button>
      )}

      {isPending && (
        <LoadingSpinner
          size="sm"
          className="ml-auto"
        />
      )}
    </div>
  )
}

export default DryRunStatusBar
