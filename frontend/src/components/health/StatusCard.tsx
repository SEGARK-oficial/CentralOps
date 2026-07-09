import type React from "react"
import { useTranslation } from "react-i18next"
import { Card } from "@/components/ui/Card/Card"
import { HealthBadge, useStatusLabel } from "@/components/health/HealthBadge"
import type { PipelineHealthStatus } from "@/types"

const STATUS_DESCRIPTION_KEY: Record<PipelineHealthStatus, string> = {
  healthy: "health.statusCard.description.healthy",
  degraded: "health.statusCard.description.degraded",
  unhealthy: "health.statusCard.description.unhealthy",
  unknown: "health.statusCard.description.unknown",
}

interface StatusCardProps {
  status: PipelineHealthStatus
}

export const StatusCard: React.FC<StatusCardProps> = ({ status }) => {
  const { t } = useTranslation("dashboard")
  const statusLabel = useStatusLabel()
  return (
    <Card
      padding="md"
      className="shadow-sm"
      data-testid="health-status-card"
    >
      <div className="flex flex-col gap-3">
        <div className="flex items-center justify-between gap-3">
          <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
            {t("health.statusCard.title")}
          </span>
          <HealthBadge status={status} size="lg" />
        </div>
        <p className="text-sm text-text-secondary" aria-label={t("health.statusCard.statusAriaLabel", { status: statusLabel[status] })}>
          {t(STATUS_DESCRIPTION_KEY[status])}
        </p>
      </div>
    </Card>
  )
}
