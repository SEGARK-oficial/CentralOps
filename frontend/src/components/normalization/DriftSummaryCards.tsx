/**
 * DriftSummaryCards
 * 3 cards mostrando contadores: Novos, Ignorados, Mapeados.
 * Cada card recebe o total de uma chamada separada com filtro de status.
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import { AlertCircleIcon, EyeOffIcon, CheckCircle2Icon } from "lucide-react"
import { Card, CardContent } from "@/components/ui/Card/Card"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { cn } from "@/lib/utils"
import { formatNumber } from "@/lib/intl"

interface SummaryCardProps {
  label: string
  count: number
  isLoading: boolean
  icon: React.ReactNode
  variant: "new" | "ignored" | "mapped"
}

const variantStyles: Record<SummaryCardProps["variant"], string> = {
  new: "text-danger-600",
  ignored: "text-warning-600",
  mapped: "text-success-600",
}

const SummaryCard: React.FC<SummaryCardProps> = ({ label, count, isLoading, icon, variant }) => {
  const { t } = useTranslation("drift")
  const ariaLabel = isLoading
    ? t("summary.cardAriaLoading", { label })
    : t("summary.cardAria", { count, label: label.toLowerCase() })
  return (
    <Card
      className="flex-1 min-w-[140px]"
      aria-label={ariaLabel}
    >
      <CardContent>
        <div className="flex items-center justify-between gap-2">
          <div className="flex flex-col gap-1">
            <span className="text-xs font-medium text-text-secondary uppercase tracking-wide">{label}</span>
            {isLoading ? (
              <LoadingSpinner size="sm" />
            ) : (
              <span className={cn("text-2xl font-bold", variantStyles[variant])}>{formatNumber(count)}</span>
            )}
          </div>
          <div className={cn("shrink-0", variantStyles[variant])} aria-hidden="true">
            {icon}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

interface DriftSummaryCardsProps {
  newCount: number
  ignoredCount: number
  mappedCount: number
  isLoading: boolean
}

export const DriftSummaryCards: React.FC<DriftSummaryCardsProps> = ({
  newCount,
  ignoredCount,
  mappedCount,
  isLoading,
}) => {
  const { t } = useTranslation("drift")
  return (
    <div className="flex flex-wrap gap-4">
      <SummaryCard
        label={t("summary.new")}
        count={newCount}
        isLoading={isLoading}
        icon={<AlertCircleIcon size={28} />}
        variant="new"
      />
      <SummaryCard
        label={t("summary.ignored")}
        count={ignoredCount}
        isLoading={isLoading}
        icon={<EyeOffIcon size={28} />}
        variant="ignored"
      />
      <SummaryCard
        label={t("summary.mapped")}
        count={mappedCount}
        isLoading={isLoading}
        icon={<CheckCircle2Icon size={28} />}
        variant="mapped"
      />
    </div>
  )
}

export default DriftSummaryCards
