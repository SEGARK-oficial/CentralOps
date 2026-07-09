import type React from "react"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import type { HealthMetric, HealthSeverity } from "@/types"

const SEVERITY_ORDER: HealthSeverity[] = ["critical", "warn", "unknown", "ok"]

const SEVERITY_BADGE_VARIANT: Record<HealthSeverity, "success" | "warning" | "danger" | "outline"> = {
  ok: "success",
  warn: "warning",
  critical: "danger",
  unknown: "outline",
}

const SEVERITY_BADGE_LABEL_KEY: Record<HealthSeverity, string> = {
  ok: "health.summaryCard.severity.healthy",
  warn: "health.summaryCard.severity.attention",
  critical: "health.summaryCard.severity.critical",
  unknown: "health.summaryCard.severity.unknown",
}

function maxSeverity(metrics: HealthMetric[]): HealthSeverity {
  for (const s of SEVERITY_ORDER) {
    if (metrics.some((m) => m.severity === s)) return s
  }
  return "ok"
}

interface HealthSummaryCardProps {
  metrics: HealthMetric[]
  onViewDetails?: () => void
}

export const HealthSummaryCard: React.FC<HealthSummaryCardProps> = ({ metrics, onViewDetails }) => {
  const { t } = useTranslation("dashboard")

  if (metrics.length === 0) {
    return (
      <Card padding="sm" className="shadow-sm">
        <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("health.summaryCard.title")}</div>
        <div className="mt-2">
          <Badge variant="outline" size="lg">{t("health.summaryCard.awaitingCollection")}</Badge>
        </div>
      </Card>
    )
  }

  const aggregated = maxSeverity(metrics)
  const nonOkCount = metrics.filter((m) => m.severity !== "ok").length
  const okCount = metrics.filter((m) => m.severity === "ok").length

  const summaryText =
    nonOkCount === 0
      ? t("health.summaryCard.summaryAllHealthy", { count: okCount })
      : t("health.summaryCard.summaryMixed", { okCount, nonOkCount })

  return (
    <Card padding="sm" className="shadow-sm">
      <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("health.summaryCard.title")}</div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <Badge variant={SEVERITY_BADGE_VARIANT[aggregated]} size="lg">
          {t(SEVERITY_BADGE_LABEL_KEY[aggregated])}
        </Badge>
        {onViewDetails && (
          <Button variant="ghost" size="xs" onClick={onViewDetails} aria-label={t("health.summaryCard.viewDetailsAriaLabel")}>
            {t("health.summaryCard.viewDetails")}
          </Button>
        )}
      </div>
      <p className="mt-1 text-xs text-text-secondary">{summaryText}</p>
    </Card>
  )
}
