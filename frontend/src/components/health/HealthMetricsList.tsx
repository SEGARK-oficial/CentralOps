import type React from "react"
import { useTranslation } from "react-i18next"
import { ClockIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card } from "@/components/ui/Card/Card"
import { iconFor } from "@/lib/icons"
import { cn } from "@/lib/utils"
import type { HealthMetric, HealthSeverity } from "@/types"

// ── Severity helpers ──────────────────────────────────────────────────────────

const SEVERITY_BADGE_VARIANT: Record<HealthSeverity, "success" | "warning" | "danger" | "outline"> = {
  ok: "success",
  warn: "warning",
  critical: "danger",
  unknown: "outline",
}

const SEVERITY_BADGE_LABEL_KEY: Record<HealthSeverity, string> = {
  ok: "health.metricsList.severity.ok",
  warn: "health.metricsList.severity.warn",
  critical: "health.metricsList.severity.critical",
  unknown: "health.metricsList.severity.unknown",
}

// ring only for critical; others are distraction-free
const CARD_RING: Record<HealthSeverity, string> = {
  ok: "",
  warn: "",
  critical: "ring-1 ring-danger-200",
  unknown: "",
}

function formatRelativeMinutes(iso: string | null | undefined, t: (key: string, opts?: Record<string, unknown>) => string): string {
  if (!iso) return ""
  const diffMs = Date.now() - new Date(iso).getTime()
  const diffM = Math.floor(diffMs / 60000)
  if (diffM < 1) return t("health.metricsList.relativeTime.justNow")
  if (diffM < 60) return t("health.metricsList.relativeTime.minutesAgo", { count: diffM })
  const diffH = Math.floor(diffM / 60)
  if (diffH < 24) return t("health.metricsList.relativeTime.hoursAgo", { count: diffH })
  return t("health.metricsList.relativeTime.daysAgo", { count: Math.floor(diffH / 24) })
}

function formatMetricValue(metric: HealthMetric): string {
  const raw = String(metric.value)
  if (metric.unit) return `${raw} ${metric.unit}`
  return raw
}

// ── Single metric card ────────────────────────────────────────────────────────

interface MetricCardProps {
  metric: HealthMetric
}

const MetricCard: React.FC<MetricCardProps> = ({ metric }) => {
  const { t } = useTranslation("dashboard")
  const Icon = iconFor(metric.icon_id)
  const ringCls = CARD_RING[metric.severity]
  const severityLabel = t(SEVERITY_BADGE_LABEL_KEY[metric.severity])

  return (
    <Card
      padding="sm"
      className={cn("shadow-sm transition-shadow", ringCls)}
      title={metric.hint ?? undefined}
      aria-label={t("health.metricsList.metricAriaLabel", { label: metric.label, value: formatMetricValue(metric), severity: severityLabel })}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <Icon
            size={16}
            className="shrink-0 text-text-tertiary"
            aria-hidden="true"
          />
          <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary truncate">
            {metric.label}
          </span>
        </div>
        <Badge
          variant={SEVERITY_BADGE_VARIANT[metric.severity]}
          size="sm"
          aria-label={t("health.metricsList.severityAriaLabel", { severity: severityLabel })}
        >
          {severityLabel}
        </Badge>
      </div>

      <div className="mt-2 text-lg font-semibold text-text">
        {formatMetricValue(metric)}
      </div>

      {metric.hint && (
        <p className="mt-1 text-xs text-text-secondary line-clamp-2">
          {metric.hint}
        </p>
      )}
    </Card>
  )
}

// ── Empty state ───────────────────────────────────────────────────────────────

interface EmptyStateProps {
  lastCollectionAt?: string | null
  lastSuccessAt?: string | null
}

const EmptyState: React.FC<EmptyStateProps> = ({ lastCollectionAt, lastSuccessAt }) => {
  const { t } = useTranslation("dashboard")
  const collectionTime = lastCollectionAt ?? lastSuccessAt
  const timeText = collectionTime
    ? t("health.metricsList.lastCollection", { relative: formatRelativeMinutes(collectionTime, t) })
    : t("health.metricsList.awaitingFirstCollection")

  return (
    <Card padding="md" className="shadow-sm">
      <div className="flex flex-col items-center gap-3 py-4 text-center">
        <ClockIcon size={32} className="text-text-tertiary" aria-hidden="true" />
        <div>
          <p className="text-sm font-medium text-text">
            {t("health.metricsList.noDetailedMetrics")}
          </p>
          <p className="mt-1 text-xs text-text-secondary">{timeText}</p>
        </div>
      </div>
    </Card>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

interface HealthMetricsListProps {
  metrics: HealthMetric[]
  lastCollectionAt?: string | null
  lastSuccessAt?: string | null
}

export const HealthMetricsList: React.FC<HealthMetricsListProps> = ({
  metrics,
  lastCollectionAt,
  lastSuccessAt,
}) => {
  const { t } = useTranslation("dashboard")
  if (metrics.length === 0) {
    return (
      <EmptyState
        lastCollectionAt={lastCollectionAt}
        lastSuccessAt={lastSuccessAt}
      />
    )
  }

  // Build groups: metrics with group field → grouped; rest → ungrouped (rendered first)
  const ungrouped = metrics.filter((m) => !m.group)
  const grouped = metrics.reduce<Record<string, HealthMetric[]>>((acc, m) => {
    if (!m.group) return acc
    if (!acc[m.group]) acc[m.group] = []
    acc[m.group].push(m)
    return acc
  }, {})
  const groupNames = Object.keys(grouped)

  return (
    <div className="space-y-5" role="region" aria-label={t("health.metricsList.ariaLabel")}>
      {/* Ungrouped metrics */}
      {ungrouped.length > 0 && (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {ungrouped.map((m) => (
            <MetricCard key={m.id} metric={m} />
          ))}
        </div>
      )}

      {/* Grouped metrics — only show section header if group has 2+ metrics */}
      {groupNames.map((groupName) => {
        const groupMetrics = grouped[groupName]
        const showHeader = groupMetrics.length >= 2
        return (
          <div key={groupName}>
            {showHeader && (
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-tertiary">
                {groupName}
              </h3>
            )}
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {groupMetrics.map((m) => (
                <MetricCard key={m.id} metric={m} />
              ))}
            </div>
          </div>
        )
      })}
    </div>
  )
}
