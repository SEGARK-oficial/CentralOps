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
import { SourceStatusBadge } from "@/components/dashboard/SourceStatusBadge"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Select } from "@/components/ui/Select/Select"
import { formatDateTime as intlFormatDateTime } from "@/lib/intl"

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


/**
 * Barra de controle: o que está em foco e sobre qual janela, numa linha só.
 * Antes eram dois cards com título e parágrafo explicando o filtro global —
 * chrome puro empurrando os números para baixo da dobra.
 */
function ScopeBar({
  organization,
  platform,
  integration,
  generatedAt,
  counts,
  days,
  onDaysChange,
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
  days: number
  onDaysChange: (days: number) => void
  onClear: () => void
}) {
  const { t } = useTranslation("dashboard")
  const allOrganizations = t("dashboardPage.scope.allOrganizations")
  const allPlatforms = t("dashboardPage.scope.allPlatforms")
  const allIntegrations = t("dashboardPage.scope.allIntegrations")
  const hasScopedFilters =
    organization !== allOrganizations || platform !== allPlatforms || integration !== allIntegrations

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-border bg-surface px-4 py-3">
      <span className="font-mono text-[11px] uppercase tracking-[0.1em] text-text-tertiary">
        {t("dashboardPage.scope.title")}
      </span>
      <Badge variant="outline" size="sm">{t("dashboardPage.scope.client", { value: organization })}</Badge>
      <Badge variant="outline" size="sm">{t("dashboardPage.scope.platform", { value: platform })}</Badge>
      <Badge variant="outline" size="sm">{t("dashboardPage.scope.integration", { value: integration })}</Badge>

      {counts && (
        <span className="font-mono text-xs text-text-tertiary">
          {t("dashboardPage.scope.counts", {
            orgs: counts.organizations,
            integrations: counts.integrations,
            active: counts.activeIntegrations,
          })}
        </span>
      )}

      {hasScopedFilters && (
        <Button variant="ghost" size="xs" onClick={onClear}>
          {t("dashboardPage.scope.clear")}
        </Button>
      )}

      <div className="ml-auto flex items-center gap-2">
        <span className="font-mono text-xs text-text-tertiary">
          {t("dashboardPage.scope.generatedAt", {
            time: formatDateTime(generatedAt, t),
            relative: formatRelativeFromNow(generatedAt, t),
          })}
        </span>
        {/* Era um <select> nativo estilizado à mão: borda na hairline (1.32:1,
            reprova a WCAG 1.4.11) e foco próprio. O componente do sistema já
            traz `border-field` e o `focus-ring` único. */}
        <Select
          id="dashboard-window"
          size="sm"
          className="w-32"
          aria-label={t("dashboardPage.timeWindow.label")}
          value={days}
          onChange={(value) => onDaysChange(Number(value))}
          options={[
            { value: 1, label: t("dashboardPage.timeWindow.options.24h") },
            { value: 7, label: t("dashboardPage.timeWindow.options.7d") },
            { value: 30, label: t("dashboardPage.timeWindow.options.30d") },
          ]}
        />
      </div>
    </div>
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
        icon={<ShieldCheckIcon size={24} />}
        title={t("dashboardPage.title")}
        actions={
          <Button variant="outline" size="sm" onClick={() => void loadSummary(true)} disabled={refreshing} leftIcon={<RefreshCwIcon size={14} />}>
            {refreshing ? t("dashboardPage.updating") : t("common:actions.refresh")}
          </Button>
        }
      />

      <ScopeBar
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
        days={days}
        onDaysChange={setDays}
        onClear={clearFilters}
      />

      <KpiGrid kpis={summary.kpis} />

      <Card padding="md">
        <div className="space-y-3">
          <h2 className="font-mono text-xs uppercase tracking-[0.1em] text-text-secondary">
            {t("dashboardPage.sourcesHealth.title")}
          </h2>

          {degradedItems.length > 0 ? (
            <div className="max-h-80 space-y-2 overflow-auto">
              {degradedItems.map((item) => (
                <button
                  key={`${item.integration_id}-${item.status}`}
                  type="button"
                  className="w-full rounded-lg border border-warning-200 bg-warning-50 px-4 py-3 text-left transition-colors hover:border-warning-500"
                  onClick={() => {
                    setSelectedIntegrationId(item.integration_id)
                    navigate(`/integrations/${item.integration_id}`)
                  }}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate font-semibold text-text">{item.integration_name}</div>
                      <div className="truncate font-mono text-xs text-text-tertiary">
                        {item.organization_name || t("dashboardPage.sourcesHealth.unnamedClient", { id: item.organization_id })}
                      </div>
                    </div>
                    <SourceStatusBadge status={item.status} />
                  </div>
                  <div className="mt-2 text-xs text-text-secondary">
                    {item.last_error || t("dashboardPage.sourcesHealth.noDetail")}
                    {item.last_checked_at ? ` · ${formatDateTime(item.last_checked_at, t)}` : ""}
                  </div>
                </button>
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-dashed border-border p-6 text-center text-sm text-text-tertiary">
              {t("dashboardPage.sourcesHealth.noDegraded")}
            </div>
          )}

          {Object.keys(byPlatform).length > 0 && (
            <div className="border-t border-border pt-3">
              <div className="font-mono text-[11px] uppercase tracking-[0.1em] text-text-tertiary">
                {t("dashboardPage.sourcesHealth.byPlatform")}
              </div>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-2">
                {Object.entries(byPlatform).map(([platform, count]) => (
                  <div key={platform} className="flex items-center gap-2">
                    <Badge variant="outline" size="sm">
                      {platform}
                    </Badge>
                    <span className="font-mono text-sm text-text">{count}</span>
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
