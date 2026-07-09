import type React from "react"
import { useTranslation } from "react-i18next"
import { Badge } from "@/components/ui/Badge/Badge"
import type { PipelineHealthStatus } from "@/types"

const STATUS_VARIANT: Record<PipelineHealthStatus, "success" | "warning" | "danger" | "outline"> = {
  healthy: "success",
  degraded: "warning",
  unhealthy: "danger",
  unknown: "outline",
}

const STATUS_LABEL_KEY: Record<PipelineHealthStatus, string> = {
  healthy: "health.badge.healthy",
  degraded: "health.badge.degraded",
  unhealthy: "health.badge.unhealthy",
  unknown: "health.badge.unknown",
}

interface HealthBadgeProps {
  status: PipelineHealthStatus
  size?: "sm" | "md" | "lg"
  className?: string
}

export const HealthBadge: React.FC<HealthBadgeProps> = ({ status, size = "md", className }) => {
  const { t } = useTranslation("dashboard")
  return (
    <Badge variant={STATUS_VARIANT[status]} size={size} className={className}>
      {t(STATUS_LABEL_KEY[status])}
    </Badge>
  )
}

export { STATUS_VARIANT }
export function useStatusLabel(): Record<PipelineHealthStatus, string> {
  const { t } = useTranslation("dashboard")
  return {
    healthy: t(STATUS_LABEL_KEY.healthy),
    degraded: t(STATUS_LABEL_KEY.degraded),
    unhealthy: t(STATUS_LABEL_KEY.unhealthy),
    unknown: t(STATUS_LABEL_KEY.unknown),
  }
}
