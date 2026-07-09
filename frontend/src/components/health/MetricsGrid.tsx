import type React from "react"
import { Link } from "react-router-dom"
import { useTranslation } from "react-i18next"
import { Card } from "@/components/ui/Card/Card"
import type { IntegrationPipelineHealth } from "@/types"

function formatLag(seconds: number | null, t: (key: string, opts?: Record<string, unknown>) => string): string {
  if (seconds === null) return "—"
  if (seconds < 60) return t("health.metricsGrid.lag.secondsAgo", { count: seconds })
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return t("health.metricsGrid.lag.minutesAgo", { count: minutes })
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return t("health.metricsGrid.lag.hoursAgo", { count: hours })
  const days = Math.floor(hours / 24)
  return t("health.metricsGrid.lag.daysAgo", { count: days })
}

interface MetricCardProps {
  label: string
  value: React.ReactNode
  testId: string
  footer?: React.ReactNode
}

const MetricCard: React.FC<MetricCardProps> = ({ label, value, testId, footer }) => (
  <Card padding="sm" className="shadow-sm" data-testid={testId}>
    <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{label}</div>
    <div className="mt-2 text-lg font-semibold text-text">{value}</div>
    {footer && <div className="mt-1.5">{footer}</div>}
  </Card>
)

interface MetricsGridProps {
  data: IntegrationPipelineHealth
}

export const MetricsGrid: React.FC<MetricsGridProps> = ({ data }) => {
  const { t } = useTranslation("dashboard")
  const driftLink = `/drift?vendor=${encodeURIComponent(String(data.integration_id))}`
  const quarantineLink = `/quarantine?integration_id=${data.integration_id}`

  return (
    <div
      className="grid grid-cols-2 gap-3"
      role="region"
      aria-label={t("health.metricsGrid.ariaLabel")}
    >
      <MetricCard
        label={t("health.metricsGrid.eventsPerMinute")}
        value={data.events_per_minute !== null ? String(data.events_per_minute) : "—"}
        testId="metrics-events-per-minute"
      />

      <MetricCard
        label={t("health.metricsGrid.collectionLag")}
        value={
          <span aria-label={t("health.metricsGrid.collectionLagAriaLabel", { value: formatLag(data.lag_seconds, t) })}>
            {formatLag(data.lag_seconds, t)}
          </span>
        }
        testId="metrics-lag"
      />

      <MetricCard
        label={t("health.metricsGrid.drift24h")}
        value={String(data.drift_count_24h)}
        testId="metrics-drift-24h"
        footer={
          <Link
            to={driftLink}
            className="text-xs text-primary-600 underline hover:text-primary-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          >
            {t("health.metricsGrid.viewInDriftExplorer")}
          </Link>
        }
      />

      <MetricCard
        label={t("health.metricsGrid.quarantine24h")}
        value={String(data.quarantine_count_24h)}
        testId="metrics-quarantine-24h"
        footer={
          <Link
            to={quarantineLink}
            className="text-xs text-primary-600 underline hover:text-primary-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          >
            {t("health.metricsGrid.viewInQuarantine")}
          </Link>
        }
      />
    </div>
  )
}
