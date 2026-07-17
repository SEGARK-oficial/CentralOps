"use client"

import type React from "react"
import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  LayoutDashboardIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
} from "lucide-react"
import * as api from "@/services/api"
import type { DashboardSummaryV2 } from "@/types"
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


function ScopeSummary({
  organization,
  platform,
  integration,
  generatedAt,
  counts,
  onClear,
}: {
  organization: string
  platform: string
  integration: string
  generatedAt?: string | null
  counts?: {
    organizations: number
    integrations: number
    activeIntegrations: number
  } | null
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
            {counts && (
              <span>
                {t("dashboardPage.scope.counts", {
                  orgs: counts.organizations,
                  integrations: counts.integrations,
                  active: counts.activeIntegrations,
                })}
              </span>
            )}
            <span>
              {t("dashboardPage.scope.generatedAt", {
                time: formatDateTime(generatedAt, t),
                relative: formatRelativeFromNow(generatedAt, t),
              })}
            </span>
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
    setSelectedIntegrationId,
  } = usePlatform()

  const [summary, setSummary] = useState<DashboardSummaryV2 | null>(null)
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

      // Fetch ÚNICA: o payload v2 consolidado carrega KPIs, buckets,
      // contagens de escopo e itens degradados numa só chamada.
      const data = await api.getDashboardSummary({
        organization_id: selectedOrgId,
        integration_id: selectedIntegrationId,
        platform: selectedPlatform,
        days,
      })
      setSummary(data)
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

  if (!summary) {
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

  const degradedItems = summary.integrations?.degraded_items ?? []
  const byPlatform = summary.integrations?.by_platform ?? {}

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
        generatedAt={summary.generated_at}
        counts={
          summary.organizations && summary.integrations
            ? {
                organizations: summary.organizations.total,
                integrations: summary.integrations.total,
                activeIntegrations: summary.integrations.active,
              }
            : null
        }
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

      <KpiGrid kpis={summary.kpis} />

      <Card className="shadow-sm">
        <div className="space-y-4 p-5">
          <div>
            <h2 className="text-lg font-semibold text-text">{t("dashboardPage.sourcesHealth.title")}</h2>
            <p className="text-sm text-text-secondary">{t("dashboardPage.sourcesHealth.description")}</p>
          </div>

          {degradedItems.length > 0 ? (
            <div className="max-h-80 space-y-2 overflow-auto">
              {degradedItems.map((item) => (
                <button
                  key={`${item.integration_id}-${item.status}`}
                  type="button"
                  className="w-full rounded-2xl border border-warning-200 bg-warning-50 px-4 py-3 text-left transition hover:border-warning-500"
                  onClick={() => {
                    setSelectedIntegrationId(item.integration_id)
                    navigate(`/integrations/${item.integration_id}`)
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

          {Object.keys(byPlatform).length > 0 && (
            <div className="rounded-2xl border border-border bg-surface p-4">
              <div className="text-sm font-semibold text-text">{t("dashboardPage.sourcesHealth.byPlatform")}</div>
              <div className="mt-3 flex flex-wrap gap-3">
                {Object.entries(byPlatform).map(([platform, count]) => (
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

      {summary.top_buckets.length > 0 && (
        <div className="grid gap-6 xl:grid-cols-2">
          {summary.top_buckets.map((section) => (
            <BucketSectionComponent key={section.id} section={section} />
          ))}
        </div>
      )}
    </div>
  )
}

export default DashboardPage
