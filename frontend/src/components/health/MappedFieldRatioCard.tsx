import type React from "react"
import { useTranslation } from "react-i18next"
import { Card } from "@/components/ui/Card/Card"
import { cn } from "@/lib/utils"

interface MappedFieldRatioCardProps {
  ratio: number | null
}

export const MappedFieldRatioCard: React.FC<MappedFieldRatioCardProps> = ({ ratio }) => {
  const { t } = useTranslation("dashboard")

  if (ratio === null) {
    return (
      <Card padding="sm" className="shadow-sm">
        <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
          {t("health.mappedFieldRatio.title")}
        </div>
        <div className="mt-2 text-sm text-text-secondary">
          {t("health.mappedFieldRatio.notCalculable")}
        </div>
      </Card>
    )
  }

  const percentage = Math.round(ratio * 100)

  const progressColor = cn(
    percentage >= 80 ? "bg-success-500" : percentage >= 50 ? "bg-warning-500" : "bg-danger-500",
  )

  return (
    <Card padding="sm" className="shadow-sm">
      <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
        {t("health.mappedFieldRatio.title")}
      </div>
      <div className="mt-2 flex items-baseline gap-2">
        <span className="text-lg font-semibold text-text">{percentage}%</span>
        <span className="text-xs text-text-secondary">{t("health.mappedFieldRatio.mappedFields")}</span>
      </div>
      <div
        className="mt-2 h-2 w-full overflow-hidden rounded-full bg-surface-tertiary"
        role="progressbar"
        aria-valuenow={percentage}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={t("health.mappedFieldRatio.ariaLabel", { percentage })}
      >
        <div
          className={cn("h-full rounded-full transition-all duration-300", progressColor)}
          style={{ width: `${percentage}%` }}
        />
      </div>
    </Card>
  )
}
