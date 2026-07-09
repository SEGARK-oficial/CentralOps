"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  AlertTriangleIcon,
  ArrowDownRightIcon,
  ArrowUpRightIcon,
  BellIcon,
  BuildingIcon,
  Clock3Icon,
  MinusIcon,
  PlugIcon,
  RefreshCwIcon,
  ShieldAlertIcon,
  ShieldCheckIcon,
  LayoutDashboardIcon,
} from "lucide-react"
import * as api from "@/services/api"
import { severityLabel } from "@/lib/labels"
import type {
  DashboardAlertBucketSummary,
  DashboardMetricComparison,
  DashboardPrioritySummary,
  DashboardSummary,
  DashboardSummaryV2,
} from "@/types"
import { usePlatform } from "@/contexts/PlatformContext"
import { BucketSectionComponent } from "@/components/dashboard/BucketSectionComponent"
import { KpiGrid } from "@/components/dashboard/KpiGrid"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { formatDateTime as intlFormatDateTime } from "@/lib/intl"

const selectCls =
  "h-9 rounded-md border border-border bg-surface px-3 text-sm text-text focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/20"
const chartColors = {
  critical: "bg-danger-600",
  high: "bg-warning-500",
  medium: "bg-primary-500",
  low: "bg-success-500",
  info: "bg-primary-400",
}

function formatDayLabel(timestamp?: string) {
  if (!timestamp) return "-"
  return intlFormatDateTime(timestamp, { day: "2-digit", month: "2-digit" })
}

function formatDateTime(value: string | null | undefined, t: (key: string) => string) {
  if (!value) return t("dashboardPage.dates.noData")
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : intlFormatDateTime(date)
}

function formatRelativeFromNow(value: string | null | undefined, t: (key: string, opts?: Record<string, unknown>) => string) {
  if (!value) return t("dashboardPage.dates.noRecentQuery")
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return t("dashboardPage.dates.invalidTime")

  const diffMs = Date.now() - timestamp
  const diffMinutes = Math.max(0, Math.round(diffMs / 60000))
  if (diffMinutes < 1) return t("dashboardPage.dates.now")
  if (diffMinutes < 60) return t("dashboardPage.dates.minutesAgo", { count: diffMinutes })
  const diffHours = Math.round(diffMinutes / 60)
  if (diffHours < 24) return t("dashboardPage.dates.hoursAgo", { count: diffHours })
  const diffDays = Math.round(diffHours / 24)
  return t("dashboardPage.dates.daysAgo", { count: diffDays })
}

function buildAlertsPath(filters: Record<string, string | number | null | undefined>) {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value === null || value === undefined || value === "") continue
    params.set(key, String(value))
  }
  const qs = params.toString()
  return `/alerts${qs ? `?${qs}` : ""}`
}

function buildWindowRange(lastQueryAt?: string | null, days = 7) {
  if (!lastQueryAt) return { from: undefined, to: undefined }
  const end = new Date(lastQueryAt)
  if (Number.isNaN(end.getTime())) return { from: undefined, to: undefined }
  const start = new Date(end.getTime() - days * 24 * 60 * 60 * 1000)
  return {
    from: start.toISOString(),
    to: end.toISOString(),
  }
}

function comparisonLabel(metric: DashboardMetricComparison, t: (key: string, opts?: Record<string, unknown>) => string) {
  if (metric.trend === "up") return t("dashboardPage.trend.up", { value: Math.abs(metric.delta) })
  if (metric.trend === "down") return t("dashboardPage.trend.down", { value: Math.abs(metric.delta) })
  return t("dashboardPage.trend.stable")
}

function TrendIndicator({ metric }: { metric: DashboardMetricComparison }) {
  const { t } = useTranslation("dashboard")
  const icon =
    metric.trend === "up" ? <ArrowUpRightIcon size={14} /> : metric.trend === "down" ? <ArrowDownRightIcon size={14} /> : <MinusIcon size={14} />
  const variant = metric.trend === "up" ? "warning" : metric.trend === "down" ? "success" : "outline"

  return (
    <Badge variant={variant} size="sm">
      <span className="flex items-center gap-1">
        {icon}
        {comparisonLabel(metric, t)}
      </span>
    </Badge>
  )
}


function ScopeSummary({
  organization,
  platform,
  integration,
  lastQueryAt,
  latestTimestamp,
  onClear,
}: {
  organization: string
  platform: string
  integration: string
  lastQueryAt?: string | null
  latestTimestamp?: string | null
  onClear: () => void
}) {
  const { t } = useTranslation("dashboard")
  const allOrganizations = t("dashboardPage.scope.allOrganizations")
  const allPlatforms = t("dashboardPage.scope.allPlatforms")
  const allIntegrations = t("dashboardPage.scope.allIntegrations")
  const hasScopedFilters =
    organization !== allOrganizations || platform !== allPlatforms || integration !== allIntegrations

  return (
    <Card className="shadow-sm">
      <div className="flex flex-col gap-4 p-5 lg:flex-row lg:items-end lg:justify-between">
        <div className="space-y-3">
          <div>
            <h2 className="text-lg font-semibold text-text">{t("dashboardPage.scope.title")}</h2>
            <p className="text-sm text-text-secondary">
              {t("dashboardPage.scope.description")}
            </p>
          </div>

          <div className="flex flex-wrap gap-2">
            <Badge variant="outline" size="sm">{t("dashboardPage.scope.client", { value: organization })}</Badge>
            <Badge variant="outline" size="sm">{t("dashboardPage.scope.platform", { value: platform })}</Badge>
            <Badge variant="outline" size="sm">{t("dashboardPage.scope.integration", { value: integration })}</Badge>
          </div>

          <div className="flex flex-wrap gap-4 text-xs text-text-secondary">
            <span>
              {t("dashboardPage.scope.lastQuery", {
                time: formatDateTime(lastQueryAt, t),
                relative: formatRelativeFromNow(lastQueryAt, t),
              })}
            </span>
            <span>{t("dashboardPage.scope.lastEvent", { time: formatDateTime(latestTimestamp, t) })}</span>
          </div>
        </div>

        {hasScopedFilters && (
          <Button variant="outline" size="sm" onClick={onClear}>
            {t("dashboardPage.scope.clear")}
          </Button>
        )}
      </div>
    </Card>
  )
}


function PriorityCard({
  title,
  description,
  item,
  onClick,
}: {
  title: string
  description: string
  item?: DashboardPrioritySummary | null
  onClick?: () => void
}) {
  const { t } = useTranslation("dashboard")
  return (
    <Card className="shadow-sm">
      <div className="space-y-4 p-5">
        <div>
          <h2 className="text-lg font-semibold text-text">{title}</h2>
          <p className="text-sm text-text-secondary">{description}</p>
        </div>

        {!item ? (
          <div className="rounded-2xl border border-dashed border-border p-6 text-center text-sm text-text-secondary">
            {t("dashboardPage.priority.empty")}
          </div>
        ) : (
          <button
            type="button"
            onClick={onClick}
            className="w-full rounded-2xl border border-border bg-surface p-4 text-left transition hover:border-primary-300 hover:bg-surface-tertiary"
          >
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <div className="truncate text-base font-semibold text-text">
                  {item.integration_name || item.organization_name || t("dashboardPage.priority.noIdentification")}
                </div>
                <div className="mt-1 text-xs text-text-secondary">
                  {item.organization_name || t("dashboardPage.priority.unnamedClient")}
                  {item.integration_name && item.organization_name !== item.integration_name ? ` · ${item.integration_name}` : ""}
                </div>
              </div>
              <Badge variant="danger" size="sm">
                {t("dashboardPage.priority.criticalCount", { count: item.critical })}
              </Badge>
            </div>

            <div className="mt-4 grid gap-2 text-sm text-text-secondary sm:grid-cols-3">
              <div>{t("dashboardPage.priority.total", { value: item.total })}</div>
              <div>{t("dashboardPage.priority.critical", { value: item.critical })}</div>
              <div>{t("dashboardPage.priority.high", { value: item.high })}</div>
            </div>
          </button>
        )}
      </div>
    </Card>
  )
}

function BucketList({
  title,
  description,
  items,
  emptyLabel,
  onSelect,
}: {
  title: string
  description: string
  items: DashboardAlertBucketSummary[]
  emptyLabel?: string
  onSelect: (bucket: DashboardAlertBucketSummary) => void
}) {
  const { t } = useTranslation("dashboard")
  return (
    <Card className="shadow-sm">
      <div className="space-y-4 p-5">
        <div>
          <h2 className="text-lg font-semibold text-text">{title}</h2>
          <p className="text-sm text-text-secondary">{description}</p>
        </div>

        {items.length === 0 ? (
          <div className="rounded-2xl border border-dashed border-border p-6 text-center text-sm text-text-secondary">
            {emptyLabel || t("dashboardPage.buckets.empty")}
          </div>
        ) : (
          <div className="space-y-2">
            {items.map((item) => (
              <button
                key={`${item.integration_id || "all"}-${item.key}-${item.label || item.key}`}
                type="button"
                onClick={() => onSelect(item)}
                className="flex w-full items-center justify-between rounded-2xl border border-border bg-surface px-4 py-3 text-left transition hover:border-primary-300 hover:bg-surface-tertiary"
              >
                <div className="min-w-0">
                  <div className="truncate font-semibold text-text">{item.label || item.key}</div>
                  {item.label && <div className="truncate text-xs text-text-secondary">{item.key}</div>}
                  {(item.organization_name || item.integration_name) && (
                    <div className="mt-1 truncate text-xs text-text-secondary">
                      {item.organization_name || t("dashboardPage.buckets.unnamedClient")}
                      {item.integration_name ? ` · ${item.integration_name}` : ""}
                    </div>
                  )}
                </div>
                <Badge variant="outline" size="sm">
                  {item.count}
                </Badge>
              </button>
            ))}
          </div>
        )}
      </div>
    </Card>
  )
}

const DashboardPage: React.FC = () => {
  const { t } = useTranslation("dashboard")
  const navigate = useNavigate()
  const {
    selectedOrgId,
    selectedPlatform,
    selectedIntegrationId,
    selectedOrganization,
    selectedIntegration,
    clearFilters,
    setSelectedOrgId,
    setSelectedIntegrationId,
  } = usePlatform()

  const [summary, setSummary] = useState<DashboardSummary | null>(null)
  const [summaryV2, setSummaryV2] = useState<DashboardSummaryV2 | null>(null)
  const [days, setDays] = useState(7)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadSummary = useCallback(async (refresh = false) => {
    try {
      setError(null)
      if (refresh) {
        setRefreshing(true)
      } else {
        setLoading(true)
      }

      const params = {
        organization_id: selectedOrgId,
        integration_id: selectedIntegrationId,
        platform: selectedPlatform,
        days,
      }

      // Fetch v1 (for chart/sources/degraded that v2 doesn't cover yet) and v2 in parallel
      const [v1Result, v2Result] = await Promise.allSettled([
        api.getDashboardSummary(params),
        api.getDashboardSummaryV2(params),
      ])
      if (v1Result.status === "fulfilled") setSummary(v1Result.value)
      if (v2Result.status === "fulfilled") setSummaryV2(v2Result.value)
      if (v1Result.status === "rejected" && v2Result.status === "rejected") {
        throw v1Result.reason
      }
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : t("dashboardPage.loadError")
      setError(message)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [days, selectedIntegrationId, selectedOrgId, selectedPlatform])

  useEffect(() => {
    void loadSummary()
  }, [loadSummary])

  const chart = useMemo(() => {
    const points = summary?.alerts.trend ?? []
    const max = Math.max(...points.map((point) => point.total), 1)
    return { points, max }
  }, [summary])

  const windowRange = useMemo(
    () => buildWindowRange(summary?.alerts.last_query_at, summary?.alerts.window_days || days),
    [days, summary?.alerts.last_query_at, summary?.alerts.window_days],
  )

  const openAlerts = useCallback((filters: Record<string, string | number | null | undefined>) => {
    const scopedFilters = { ...filters }
    if (!scopedFilters.time_from && windowRange.from) scopedFilters.time_from = windowRange.from
    if (!scopedFilters.time_to && windowRange.to) scopedFilters.time_to = windowRange.to
    navigate(buildAlertsPath(scopedFilters))
  }, [navigate, windowRange.from, windowRange.to])

  if (loading) {
    return <LoadingSpinner size="lg" text={t("dashboardPage.loading")} className="py-20" />
  }

  if (error) {
    return (
      <Notice variant="danger" title={t("dashboardPage.loadError")}>
        {error}
      </Notice>
    )
  }

  if (!summary && !summaryV2) {
    return (
      <EmptyState
        icon={<LayoutDashboardIcon size={48} />}
        title={t("dashboardPage.empty.title")}
        description={t("dashboardPage.empty.description")}
        action={
          <Button variant="outline" size="sm" onClick={() => void loadSummary(true)} disabled={refreshing} leftIcon={<RefreshCwIcon size={14} />}>
            {refreshing ? t("dashboardPage.updating") : t("common:actions.refresh")}
          </Button>
        }
        className="py-20"
      />
    )
  }

  // Stats array for v1 fallback rendering (used only when summaryV2 is unavailable)
  const { organizations: orgs, integrations: ints, alerts } = summary ?? {
    organizations: { total: 0, active: 0 },
    integrations: { total: 0, active: 0, authenticated: 0, by_platform: {}, health: { healthy: 0, degraded: 0, error: 0, unknown: 0 }, degraded_items: [], comparison: { degraded_integrations: { current: 0, previous: 0, delta: 0, trend: "stable" as const } } },
    alerts: { total: 0, by_severity: { critical: 0, high: 0, medium: 0, low: 0, info: 0 }, trend: [], sources: [], top_hosts: [], top_rules: [], top_mitre_ids: [], top_agent_groups: [], partial_errors: [], latest_timestamp: null, last_query_at: null, unsupported_sources: 0, window_days: 7, applied_organization_id: null, applied_integration_id: null, applied_platform: null, comparison: { total_alerts: { current: 0, previous: 0, delta: 0, trend: "stable" as const }, critical_alerts: { current: 0, previous: 0, delta: 0, trend: "stable" as const } }, most_critical_client: null, most_critical_integration: null },
  }
  const stats = [
    {
      label: t("dashboardPage.stats.organizationsInScope"),
      value: orgs.total,
      sub: t("dashboardPage.stats.organizationsActive", { count: orgs.active }),
      icon: BuildingIcon,
      badge: "primary" as const,
    },
    {
      label: t("dashboardPage.stats.integrationsInScope"),
      value: ints.total,
      sub: t("dashboardPage.stats.integrationsSummary", {
        active: ints.active,
        healthy: ints.health.healthy,
        degraded: ints.health.degraded,
        error: ints.health.error,
        unknown: ints.health.unknown,
        inactive: ints.health.inactive ?? Math.max(0, ints.total - ints.active),
      }),
      icon: PlugIcon,
      badge: "outline" as const,
    },
    {
      label: t("dashboardPage.stats.alertsInPeriod"),
      value: alerts.total,
      sub: t("dashboardPage.stats.alertsDays", { count: alerts.window_days }),
      icon: BellIcon,
      badge: "primary" as const,
      comparison: alerts.comparison.total_alerts,
    },
    {
      label: t("dashboardPage.stats.critical"),
      value: alerts.by_severity.critical,
      sub: t("dashboardPage.stats.criticalSub"),
      icon: AlertTriangleIcon,
      badge: "danger" as const,
      comparison: alerts.comparison.critical_alerts,
    },
    {
      label: t("dashboardPage.stats.degradedEnvironments"),
      value: ints.degraded_items.length,
      sub: t("dashboardPage.stats.degradedSub"),
      icon: ShieldAlertIcon,
      badge: "warning" as const,
      comparison: ints.comparison.degraded_integrations,
    },
    {
      label: t("dashboardPage.stats.lastEvent"),
      value: alerts.latest_timestamp ? formatDateTime(alerts.latest_timestamp, t) : t("dashboardPage.stats.noData"),
      sub: alerts.latest_timestamp ? formatRelativeFromNow(alerts.latest_timestamp, t) : t("dashboardPage.stats.freshness"),
      icon: Clock3Icon,
      badge: "default" as const,
    },
  ]

  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("dashboardPage.eyebrow")}
        icon={<ShieldCheckIcon size={24} />}
        title={t("dashboardPage.title")}
        description={t("dashboardPage.description")}
        actions={
          <Button variant="outline" size="sm" onClick={() => void loadSummary(true)} disabled={refreshing} leftIcon={<RefreshCwIcon size={14} />}>
            {refreshing ? t("dashboardPage.updating") : t("common:actions.refresh")}
          </Button>
        }
      />

      <ScopeSummary
        organization={selectedOrganization?.name || t("dashboardPage.scope.allOrganizations")}
        platform={selectedPlatform || t("dashboardPage.scope.allPlatforms")}
        integration={selectedIntegration?.name || t("dashboardPage.scope.allIntegrations")}
        lastQueryAt={alerts.last_query_at}
        latestTimestamp={alerts.latest_timestamp}
        onClear={clearFilters}
      />

      <Card className="shadow-sm">
        <div className="flex flex-col gap-4 p-5 lg:flex-row lg:items-end lg:justify-between">
          <div>
            <h2 className="text-lg font-semibold text-text">{t("dashboardPage.timeWindow.title")}</h2>
            <p className="text-sm text-text-secondary">{t("dashboardPage.timeWindow.description")}</p>
          </div>

          <div className="flex flex-col gap-1.5 lg:min-w-[180px]">
            <label htmlFor="dashboard-window" className="text-sm font-medium text-text">{t("dashboardPage.timeWindow.label")}</label>
            <select id="dashboard-window" value={days} onChange={(event) => setDays(Number(event.target.value))} className={selectCls}>
              <option value={1}>{t("dashboardPage.timeWindow.options.24h")}</option>
              <option value={7}>{t("dashboardPage.timeWindow.options.7d")}</option>
              <option value={30}>{t("dashboardPage.timeWindow.options.30d")}</option>
            </select>
          </div>
        </div>
      </Card>

      {alerts.partial_errors.length > 0 && (
        <Notice variant="warning" title={t("dashboardPage.notices.partialErrorsTitle")}>
          {alerts.partial_errors.join(" | ")}
        </Notice>
      )}

      {alerts.unsupported_sources > 0 && (
        <Notice variant="info" title={t("dashboardPage.notices.unsupportedSourcesTitle")}>
          {t("dashboardPage.notices.unsupportedSources", { count: alerts.unsupported_sources })}
        </Notice>
      )}

      {/* KPI cards: use v2 data-driven grid when available, fall back to v1 hardcoded */}
      {summaryV2 ? (
        <KpiGrid kpis={summaryV2.kpis} />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {stats.map((item) => (
            <Card key={item.label} padding="sm" className="shadow-sm">
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{item.label}</div>
                  <div className="mt-2 text-2xl font-bold text-text">{item.value}</div>
                  <div className="mt-1 text-xs text-text-secondary">{item.sub}</div>
                </div>
                <div className="flex flex-col items-end gap-2">
                  <item.icon size={18} className="text-text-tertiary" />
                  {typeof item.value === "number" && (
                    <Badge variant={item.badge} size="sm">
                      {item.value}
                    </Badge>
                  )}
                </div>
              </div>
              {item.comparison && (
                <div className="mt-4 flex items-center justify-between border-t border-border pt-3 text-xs text-text-secondary">
                  <span>{t("dashboardPage.stats.previousPeriod", { value: item.comparison.previous })}</span>
                  <TrendIndicator metric={item.comparison} />
                </div>
              )}
            </Card>
          ))}
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-2">
        <PriorityCard
          title={t("dashboardPage.priority.mostCriticalClient.title")}
          description={t("dashboardPage.priority.mostCriticalClient.description")}
          item={alerts.most_critical_client}
          onClick={() => {
            if (!alerts.most_critical_client?.organization_id) return
            setSelectedOrgId(alerts.most_critical_client.organization_id)
            setSelectedIntegrationId(null)
            navigate("/alerts")
          }}
        />

        <PriorityCard
          title={t("dashboardPage.priority.mostCriticalIntegration.title")}
          description={t("dashboardPage.priority.mostCriticalIntegration.description")}
          item={alerts.most_critical_integration}
          onClick={() => {
            if (!alerts.most_critical_integration?.integration_id) return
            setSelectedIntegrationId(alerts.most_critical_integration.integration_id)
            openAlerts({ integration_id: alerts.most_critical_integration.integration_id })
          }}
        />
      </div>

      <div className="grid gap-6 md:grid-cols-1 lg:grid-cols-2 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.95fr)]">
        <Card className="shadow-sm">
          <div className="space-y-5 p-5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold text-text">{t("dashboardPage.alertsTrend.title")}</h2>
                <p className="text-sm text-text-secondary">{t("dashboardPage.alertsTrend.description")}</p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Badge variant="danger" size="sm">{t("dashboardPage.alertsTrend.severity.critical")}</Badge>
                <Badge variant="warning" size="sm">{t("dashboardPage.alertsTrend.severity.high")}</Badge>
                <Badge variant="primary" size="sm">{t("dashboardPage.alertsTrend.severity.medium")}</Badge>
                <Badge variant="success" size="sm">{t("dashboardPage.alertsTrend.severity.low")}</Badge>
                <Badge variant="outline" size="sm">{t("dashboardPage.alertsTrend.severity.info")}</Badge>
              </div>
            </div>

            {chart.points.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-border p-10 text-center text-sm text-text-secondary">
                {t("dashboardPage.alertsTrend.empty")}
              </div>
            ) : (
              <div className="space-y-4">
                <div className="flex h-72 items-end gap-3 overflow-x-auto rounded-2xl border border-border bg-surface-tertiary/40 p-4">
                  {chart.points.map((point) => {
                    const totalHeight = point.total > 0 ? Math.max((point.total / chart.max) * 100, 8) : 0
                    return (
                      <button
                        key={`${point.timestamp}-${point.total}`}
                        type="button"
                        aria-label={t("dashboardPage.alertsTrend.barAriaLabel", { day: formatDayLabel(point.timestamp), count: point.total })}
                        onClick={() => {
                          if (!point.timestamp) return
                          const start = new Date(point.timestamp)
                          const end = new Date(start.getTime() + 24 * 60 * 60 * 1000)
                          openAlerts({
                            integration_id: selectedIntegrationId,
                            time_from: start.toISOString(),
                            time_to: end.toISOString(),
                          })
                        }}
                        className="flex min-w-16 flex-1 flex-col items-center gap-2 rounded-xl px-1 py-2 transition hover:bg-surface"
                      >
                        <div className="text-xs font-semibold text-text-secondary">{point.total}</div>
                        <div className="flex h-52 w-full items-end justify-center">
                          <div className="flex h-full w-10 flex-col-reverse overflow-hidden rounded-t-2xl bg-surface">
                            {(["critical", "high", "medium", "low", "info"] as const).map((severity) => {
                              const count = point[severity]
                              if (!count || point.total === 0) return null
                              return (
                                <div
                                  key={severity}
                                  className={chartColors[severity]}
                                  style={{ height: `${(count / point.total) * totalHeight}%` }}
                                  title={`${severityLabel(severity)}: ${count}`}
                                />
                              )
                            })}
                          </div>
                        </div>
                        <div className="text-xs text-text-secondary">{formatDayLabel(point.timestamp)}</div>
                      </button>
                    )
                  })}
                </div>
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                  {(["critical", "high", "medium", "low", "info"] as const).map((severity) => (
                    <button
                      key={severity}
                      type="button"
                      className="rounded-xl border border-border bg-surface p-3 text-left transition hover:border-primary-300 hover:bg-surface-tertiary"
                      onClick={() => openAlerts({ integration_id: selectedIntegrationId, severity })}
                    >
                      <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{severityLabel(severity)}</div>
                      <div className="mt-2 text-xl font-bold text-text">{alerts.by_severity[severity]}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Card>

        <Card className="shadow-sm">
          <div className="space-y-4 p-5">
            <div>
              <h2 className="text-lg font-semibold text-text">{t("dashboardPage.sourcesHealth.title")}</h2>
              <p className="text-sm text-text-secondary">{t("dashboardPage.sourcesHealth.description")}</p>
            </div>

            {ints.degraded_items.length > 0 ? (
              <div className="max-h-80 space-y-2 overflow-auto">
                {ints.degraded_items.map((item) => (
                  <button
                    key={`${item.integration_id}-${item.status}`}
                    type="button"
                    className="w-full rounded-2xl border border-warning-200 bg-warning-50 px-4 py-3 text-left transition hover:border-warning-500"
                    onClick={() => {
                      setSelectedIntegrationId(item.integration_id)
                      openAlerts({ integration_id: item.integration_id })
                    }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-semibold text-text">{item.integration_name}</div>
                        <div className="text-xs text-text-secondary">
                          {item.organization_name || t("dashboardPage.sourcesHealth.unnamedClient", { id: item.organization_id })}
                        </div>
                      </div>
                      <Badge variant="warning" size="sm">
                        {item.status}
                      </Badge>
                    </div>
                    <div className="mt-2 text-xs text-text-secondary">
                      {item.last_error || t("dashboardPage.sourcesHealth.noDetail")}
                      {item.last_checked_at ? ` · ${formatDateTime(item.last_checked_at, t)}` : ""}
                    </div>
                  </button>
                ))}
              </div>
            ) : (
              <div className="rounded-2xl border border-dashed border-border p-6 text-center text-sm text-text-secondary">
                {t("dashboardPage.sourcesHealth.noDegraded")}
              </div>
            )}

            {alerts.sources.length === 0 ? (
              <div className="rounded-2xl border border-dashed border-border p-8 text-center text-sm text-text-secondary">
                {t("dashboardPage.sourcesHealth.noSources")}
              </div>
            ) : (
              <div className="max-h-96 space-y-3 overflow-auto">
                {alerts.sources.map((source) => (
                  <button
                    key={source.integration_id}
                    type="button"
                    className="w-full rounded-2xl border border-border bg-surface p-4 text-left transition hover:border-primary-300 hover:bg-surface-tertiary"
                    onClick={() => {
                      setSelectedIntegrationId(source.integration_id)
                      openAlerts({ integration_id: source.integration_id })
                    }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div className="font-semibold text-text">{source.integration_name}</div>
                        <div className="text-xs text-text-secondary">
                          {source.organization_name || t("dashboardPage.sourcesHealth.unnamedClient", { id: source.organization_id })}
                        </div>
                      </div>
                      <Badge variant="outline" size="sm">
                        {source.total}
                      </Badge>
                    </div>
                    <div className="mt-3 grid grid-cols-2 gap-2 text-xs text-text-secondary">
                      <div>{t("dashboardPage.sourcesHealth.critical", { value: source.by_severity.critical })}</div>
                      <div>{t("dashboardPage.sourcesHealth.high", { value: source.by_severity.high })}</div>
                      <div>{t("dashboardPage.sourcesHealth.medium", { value: source.by_severity.medium })}</div>
                      <div>{t("dashboardPage.sourcesHealth.low", { value: source.by_severity.low })}</div>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {Object.keys(ints.by_platform).length > 0 && (
              <div className="rounded-2xl border border-border bg-surface p-4">
                <div className="text-sm font-semibold text-text">{t("dashboardPage.sourcesHealth.byPlatform")}</div>
                <div className="mt-3 flex flex-wrap gap-3">
                  {Object.entries(ints.by_platform).map(([platform, count]) => (
                    <div key={platform} className="flex items-center gap-2">
                      <Badge variant="outline" size="sm">
                        {platform}
                      </Badge>
                      <span className="text-sm font-semibold text-text">{count}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </Card>
      </div>

      {/* Bucket lists: use v2 data-driven sections when available, fall back to v1 hardcoded */}
      {summaryV2 && summaryV2.top_buckets.length > 0 ? (
        <div className="grid gap-6 xl:grid-cols-2">
          {summaryV2.top_buckets.map((section) => (
            <BucketSectionComponent key={section.id} section={section} />
          ))}
        </div>
      ) : (
        <div className="grid gap-6 xl:grid-cols-2">
          <BucketList
            title={t("dashboardPage.buckets.topHosts.title")}
            description={t("dashboardPage.buckets.topHosts.description")}
            items={alerts.top_hosts}
            onSelect={(bucket) => openAlerts({ integration_id: bucket.integration_id || selectedIntegrationId, hostname: bucket.key })}
          />

          <BucketList
            title={t("dashboardPage.buckets.topRules.title")}
            description={t("dashboardPage.buckets.topRules.description")}
            items={alerts.top_rules}
            onSelect={(bucket) => openAlerts({ integration_id: bucket.integration_id || selectedIntegrationId, rule_id: bucket.key })}
          />

          <BucketList
            title={t("dashboardPage.buckets.topMitre.title")}
            description={t("dashboardPage.buckets.topMitre.description")}
            items={alerts.top_mitre_ids}
            onSelect={(bucket) => openAlerts({ integration_id: bucket.integration_id || selectedIntegrationId, query: `rule.mitre.id:${bucket.key}` })}
          />

          <BucketList
            title={t("dashboardPage.buckets.topAgentGroups.title")}
            description={t("dashboardPage.buckets.topAgentGroups.description")}
            items={alerts.top_agent_groups}
            onSelect={(bucket) => openAlerts({ integration_id: bucket.integration_id || selectedIntegrationId, query: `agent.groups:\"${bucket.key}\"` })}
          />
        </div>
      )}
    </div>
  )
}

export default DashboardPage
