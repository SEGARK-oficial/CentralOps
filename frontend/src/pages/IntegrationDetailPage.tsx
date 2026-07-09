import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  ActivityIcon,
  ArrowLeftIcon,
  BellIcon,
  DatabaseIcon,
  ExternalLinkIcon,
  HeartPulseIcon,
  LayoutDashboardIcon,
  PencilIcon,
  ServerIcon,
  SettingsIcon,
} from "lucide-react"
import * as api from "@/services/api"
import type { Alert, AlertDetail, HealthResponse, Integration, IntegrationHealth, IntegrationOverview, LicensedProduct, QueryCapabilityRead } from "@/types"
import AlertDetailsDrawer from "@/components/alerts/AlertDetailsDrawer"
import { IntegrationBackfillPanel } from "@/components/backfill/IntegrationBackfillPanel"
import { HealthMetricsList } from "@/components/health/HealthMetricsList"
import { HealthSummaryCard } from "@/components/health/HealthSummaryCard"
import { IntegrationHealthPanel } from "@/components/health/IntegrationHealthPanel"
import { IntegrationDestinationsTab } from "@/components/integrations/IntegrationDestinationsTab"
import { IngestSourcePanel } from "@/components/integrations/IngestSourcePanel"
import { IntegrationDetailExtraPanels } from "@/ee/integrationDetailSlots"
import { IntegrationForm } from "@/components/integrations/IntegrationForm"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { HelpTooltip } from "@/components/ui/HelpTooltip/HelpTooltip"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/Tabs/Tabs"
import { useAuth } from "@/contexts/AuthContext"
import { usePlatform } from "@/contexts/PlatformContext"
import { DEFAULT_ALERT_INDEX, getAlertDetailFilters, getAlertRequestErrorMessage, withDefaultAlertIndex } from "@/lib/alerts"
import { authStatusLabel, authStatusVariant } from "@/lib/labels"
import { formatDateTime as formatDateTimeIntl } from "@/lib/intl"

type Tab = "overview" | "alerts" | "health" | "pipeline-health" | "destinations" | "config" | "backfill"

const LIST_PAGE_SIZE = 50

const SEVERITY_VARIANT: Record<string, "danger" | "warning" | "default" | "success" | "primary"> = {
  critical: "danger",
  high: "warning",
  medium: "default",
  low: "success",
  info: "primary",
}

const thCls = "px-4 py-3 text-left text-xs font-semibold text-text-secondary uppercase tracking-wider"
const tdCls = "px-4 py-3 text-sm"

function buildAlertsPath(integrationId: number, filters?: Record<string, string | undefined>) {
  const params = new URLSearchParams({ integration_id: String(integrationId), index: DEFAULT_ALERT_INDEX })
  for (const [key, value] of Object.entries(filters || {})) {
    if (!value) continue
    params.set(key, value)
  }
  return `/alerts?${params.toString()}`
}

const IntegrationDetailPage: React.FC = () => {
  const { t } = useTranslation("integrations")
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const { user } = useAuth()
  const { setSelectedIntegrationId } = usePlatform()
  const integrationId = Number(id)
  const isAdmin = user?.role === "admin"

  const formatDateTime = useCallback(
    (value?: string | null) => {
      if (!value) return t("list.never")
      const date = new Date(value)
      return Number.isNaN(date.getTime()) ? value : formatDateTimeIntl(date)
    },
    [t],
  )

  const [integration, setIntegration] = useState<Integration | null>(null)
  const [activeTab, setActiveTab] = useState<Tab>("overview")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [overview, setOverview] = useState<IntegrationOverview | null>(null)
  const [overviewError, setOverviewError] = useState<string | null>(null)
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [health, setHealth] = useState<IntegrationHealth | null>(null)
  const [healthV2, setHealthV2] = useState<HealthResponse | null>(null)
  const [tabLoading, setTabLoading] = useState(false)
  const [editingOpen, setEditingOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [selectedAlert, setSelectedAlert] = useState<Alert | null>(null)
  const [alertDetail, setAlertDetail] = useState<AlertDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const alertListFilters = useMemo(() => withDefaultAlertIndex({ limit: LIST_PAGE_SIZE }), [])
  const [queryCaps, setQueryCaps] = useState<QueryCapabilityRead[]>([])

  useEffect(() => {
    if (!isAdmin) return
    let cancelled = false
    api.listQueryCapabilities().then((data) => {
      if (!cancelled) setQueryCaps(data)
    }).catch(() => {/* silencia */})
    return () => { cancelled = true }
  }, [isAdmin])

  useEffect(() => {
    const load = async () => {
      try {
        setLoading(true)
        setError(null)
        const data = await api.getIntegration(integrationId)
        setIntegration(data)
        setSelectedIntegrationId(data.id)
      } catch (loadError) {
        const message = loadError instanceof Error ? loadError.message : t("detail.loadIntegrationError")
        setError(message)
      } finally {
        setLoading(false)
      }
    }

    if (!Number.isNaN(integrationId)) {
      void load()
    }
  }, [integrationId, setSelectedIntegrationId, t])

  useEffect(() => {
    if (!isAdmin && activeTab === "config") {
      setActiveTab("overview")
    }
  }, [activeTab, isAdmin])

  const loadTabData = useCallback(async (tab: Tab) => {
    setTabLoading(true)
    try {
      setError(null)
      switch (tab) {
        case "overview": {
          setOverviewError(null)
          const [overviewData, v2HealthData] = await Promise.allSettled([
            api.getIntegrationOverview(integrationId),
            api.getIntegrationHealthV2(integrationId),
          ])
          if (overviewData.status === "fulfilled") {
            setOverview(overviewData.value)
          } else {
            const reason = overviewData.reason
            setOverviewError(reason instanceof Error ? reason.message : t("detail.overviewLoadError"))
          }
          if (v2HealthData.status === "fulfilled") setHealthV2(v2HealthData.value)
          break
        }
        case "alerts":
          setAlerts((await api.listAlerts(integrationId, alertListFilters)).items)
          break
        case "health": {
          const [legacyHealth, v2Health] = await Promise.allSettled([
            api.getIntegrationHealth(integrationId),
            api.getIntegrationHealthV2(integrationId),
          ])
          if (legacyHealth.status === "fulfilled") setHealth(legacyHealth.value)
          if (v2Health.status === "fulfilled") setHealthV2(v2Health.value)
          break
        }
        default:
          break
      }
    } catch (tabError) {
      const message = tab === "alerts"
        ? getAlertRequestErrorMessage(tabError, t("detail.loadTabError"))
        : tabError instanceof Error
          ? tabError.message
          : t("detail.loadTabError")
      setError(message)
    } finally {
      setTabLoading(false)
    }
  }, [alertListFilters, integrationId, t])

  useEffect(() => {
    if (integration) {
      void loadTabData(activeTab)
    }
  }, [activeTab, integration, loadTabData])

  useEffect(() => {
    if (!selectedAlert) {
      setAlertDetail(null)
      setDetailLoading(false)
      setDetailError(null)
      return
    }

    const loadDetail = async () => {
      try {
        setDetailLoading(true)
        setDetailError(null)
        setAlertDetail(selectedAlert)
        const detail = await api.getAlertDetail(
          integrationId,
          selectedAlert.alert_id,
          getAlertDetailFilters(selectedAlert, alertListFilters),
        )
        setAlertDetail(detail)
      } catch (detailLoadError) {
        const message = getAlertRequestErrorMessage(detailLoadError, t("detail.alertDetailLoadError"))
        setDetailError(message)
        setAlertDetail(selectedAlert)
      } finally {
        setDetailLoading(false)
      }
    }

    void loadDetail()
  }, [alertListFilters, integrationId, selectedAlert, t])

  const tabs: { key: Tab; label: string; icon: React.ReactNode; show: boolean }[] = useMemo(
    () => [
      { key: "overview", label: t("detail.tabs.overview"), icon: <LayoutDashboardIcon size={16} />, show: true },
      { key: "alerts", label: t("detail.tabs.alerts"), icon: <BellIcon size={16} />, show: integration?.capabilities.includes("alerts:list") ?? false },
      { key: "health", label: t("detail.tabs.health"), icon: <HeartPulseIcon size={16} />, show: true },
      { key: "pipeline-health", label: t("detail.tabs.pipelineHealth"), icon: <ActivityIcon size={16} />, show: true },
      { key: "destinations", label: t("detail.tabs.destinations"), icon: <ServerIcon size={16} />, show: true },
      { key: "backfill", label: t("detail.tabs.backfill"), icon: <DatabaseIcon size={16} />, show: true },
      { key: "config", label: t("detail.tabs.config"), icon: <SettingsIcon size={16} />, show: Boolean(isAdmin) },
    ],
    [integration?.capabilities, isAdmin, t],
  )

  // healthCards removed — replaced by HealthMetricsList + HealthSummaryCard

  if (loading) return <LoadingSpinner size="lg" text={t("common:loading")} className="py-20" />
  if (error && !integration) {
    return (
      <div className="space-y-4">
        <button onClick={() => navigate("/integrations")} className="flex items-center gap-1.5 text-sm text-text-secondary transition-colors hover:text-primary-600">
          <ArrowLeftIcon size={16} /> {t("detail.backToIntegrations")}
        </button>
        <Notice
          variant="danger"
          title={t("detail.loadErrorTitle")}
          action={
            <Button variant="outline" size="sm" onClick={() => navigate(0)}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {error}
        </Notice>
      </div>
    )
  }
  if (!integration) {
    return (
      <EmptyState
        icon={<SettingsIcon size={40} />}
        title={t("detail.notFoundTitle")}
        description={t("detail.notFoundDescription")}
        action={
          <Button variant="outline" leftIcon={<ArrowLeftIcon size={16} />} onClick={() => navigate("/integrations")}>
            {t("detail.backToIntegrations")}
          </Button>
        }
      />
    )
  }

  return (
    <div className="space-y-6">
      <div className="space-y-4">
        <button onClick={() => navigate("/integrations")} className="flex items-center gap-1.5 text-sm text-text-secondary transition-colors hover:text-primary-600">
          <ArrowLeftIcon size={16} /> {t("detail.backToIntegrations")}
        </button>

        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-2xl font-bold text-text">{integration.name}</h1>
              <Badge variant={integration.platform === "sophos" ? "primary" : "success"}>{integration.platform}</Badge>
              <Badge variant={integration.is_active ? "success" : "warning"} size="sm">
                {integration.is_active ? t("form.statusActive") : t("form.statusInactive")}
              </Badge>
              <Badge variant={authStatusVariant(integration.auth_status)} size="sm">
                {authStatusLabel(integration.auth_status)}
              </Badge>
            </div>
            <p className="text-sm text-text-secondary">
              {integration.organization_name}
              {integration.platform === "sophos" && integration.region && ` · ${t("detail.region")}: ${integration.region}`}
              {integration.platform === "wazuh" && isAdmin && integration.manager_url && ` · ${integration.manager_url}`}
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {integration.capabilities.includes("alerts:list") && (
              <Button
                variant="outline"
                leftIcon={<ExternalLinkIcon size={16} />}
                onClick={() => navigate(buildAlertsPath(integration.id))}
              >
                {t("detail.viewAllInAlerts")}
              </Button>
            )}
            {isAdmin && (
              <Button variant="outline" onClick={() => setEditingOpen(true)} leftIcon={<PencilIcon size={16} />}>
                {t("detail.editIntegration")}
              </Button>
            )}
          </div>
        </div>

        {!integration.is_active && (
          <Notice variant="warning" title={t("detail.inactiveTitle")}>
            {t("detail.inactiveDescription")}
          </Notice>
        )}

        {integration.last_error && (
          <details className="rounded-2xl border border-border bg-surface shadow-sm">
            <summary className="cursor-pointer list-none px-5 py-4 text-sm font-semibold text-text">
              {t("detail.viewTechnicalError")}
            </summary>
            <div className="border-t border-border px-5 py-4">
              <pre className="max-h-96 overflow-auto rounded-lg bg-surface-tertiary p-4 font-mono text-xs">
                {integration.last_error}
              </pre>
            </div>
          </details>
        )}
      </div>

      <Tabs value={activeTab} onValueChange={(v) => setActiveTab(v as Tab)}>
        <TabsList ariaLabel={t("detail.tabsAriaLabel")}>
          {tabs.filter((tab) => tab.show).map((tab) => (
            <TabsTrigger key={tab.key} value={tab.key} icon={tab.icon}>
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
      </Tabs>

      {error && (
        <Notice variant="danger" title={t("detail.loadFailedTitle")}>
          {error}
        </Notice>
      )}

      {tabLoading && <LoadingSpinner size="md" text={t("common:loading")} className="py-12" />}

      {/* open-core: Enterprise integration-detail panels (Sophos partner /
          organization auto-discovery — reseller multi-tenant management) inject here via
          the @/ee/integrationDetailSlots seam. Empty in Community; the web-ee overlay
          renders PartnerTenantsPanel for partner/organization kinds. */}
      <IntegrationDetailExtraPanels
        integration={integration}
        isAdmin={isAdmin}
        onRefreshIntegration={async () => {
          try {
            const fresh = await api.getIntegration(integrationId)
            setIntegration(fresh)
          } catch {
            // non-fatal; the panel keeps polling on its own
          }
        }}
      />

      {/* Ingestão push — auto-oculta para fontes pull. */}
      {activeTab === "overview" && (
        <IngestSourcePanel integrationId={integrationId} platform={integration.platform} canManage={isAdmin} />
      )}

      {activeTab === "overview" && !tabLoading && overviewError && (
        <Notice
          variant="danger"
          title={t("detail.overviewErrorTitle")}
          action={
            <Button variant="outline" size="sm" onClick={() => void loadTabData("overview")}>
              {t("common:actions.retry")}
            </Button>
          }
        >
          {overviewError}
        </Notice>
      )}

      {activeTab === "overview" && !tabLoading && overview && (
        <div className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <HealthSummaryCard
              metrics={healthV2?.metrics ?? []}
              onViewDetails={() => setActiveTab("health")}
            />
            <Card padding="sm" className="shadow-sm">
              <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("detail.lastCheck")}</div>
              <div className="mt-2 text-lg font-semibold text-text">{formatDateTime(integration.last_checked_at)}</div>
            </Card>
            <Card padding="sm" className="shadow-sm">
              <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("detail.alertsPreview")}</div>
              <div className="mt-2 text-lg font-semibold text-text">{overview.alerts_preview?.items.length || 0}</div>
            </Card>
          </div>

          {overview.alerts_preview_error && (
            <Notice variant="warning" title={t("detail.alertsPreviewUnavailableTitle")}>
              {overview.alerts_preview_error.message}
            </Notice>
          )}

          <div className="grid gap-6">
            <Card padding="md" className="space-y-3 shadow-sm">
              <div className="flex items-center justify-between gap-3">
                <h3 className="font-semibold text-text">{t("detail.recentAlerts")}</h3>
                {integration.capabilities.includes("alerts:list") && (
                  <Button variant="outline" size="sm" onClick={() => navigate(buildAlertsPath(integration.id))}>
                    {t("detail.viewAll")}
                  </Button>
                )}
              </div>
              {overview.alerts_preview?.items?.length ? (
                <div className="space-y-2">
                  {overview.alerts_preview.items.map((alert) => (
                    <button
                      key={alert.alert_id}
                      type="button"
                      onClick={() => setSelectedAlert({ ...alert, platform: integration.platform, rule_groups: [], mitre_ids: [], mitre_tactics: [], mitre_techniques: [], agent_labels: {}, data_fields: {}, highlights: {}, raw: {}, source_index: alert.source_index, integration_id: integration.id, integration_name: integration.name })}
                      className="flex w-full items-center justify-between rounded-xl border border-border bg-surface-tertiary/40 px-3 py-2 text-left text-sm transition hover:border-primary-300"
                    >
                      <div className="min-w-0">
                        <div className="truncate font-medium text-text">{alert.title}</div>
                        <div className="text-xs text-text-secondary">{alert.timestamp ? formatDateTimeIntl(alert.timestamp) : t("detail.noTimestamp")}</div>
                      </div>
                      <Badge variant={SEVERITY_VARIANT[alert.severity] || "default"} size="sm">
                        {alert.severity}
                      </Badge>
                    </button>
                  ))}
                </div>
              ) : (
                <div className="text-sm text-text-secondary">{t("detail.noAlertsPreview")}</div>
              )}
            </Card>
          </div>

          {Array.isArray(overview.licensed_products) && (
            <Card padding="md" className="space-y-3 shadow-sm">
              <h3 className="font-semibold text-text">{t("detail.licensedProducts.title")}</h3>
              {(overview.licensed_products as LicensedProduct[]).length === 0 ? (
                <p className="text-sm text-text-secondary">{t("detail.licensedProducts.none")}</p>
              ) : (
                <>
                  {(() => {
                    const products = overview.licensed_products as LicensedProduct[]
                    const hasXdr = products.some((p) => p.category === "xdr")
                    const hasMdr = products.some((p) => p.category === "mdr")
                    // /detections/v1 aceita acesso com XDR OU MDR — o time SOC da
                    // Sophos usa o mesmo endpoint internamente, então MDR Complete
                    // também habilita. Validado empiricamente em tenants reais.
                    const detectionsLabel =
                      !hasXdr && !hasMdr
                        ? t("detail.licensedProducts.statusNotLicensed")
                        : hasXdr && hasMdr
                          ? t("detail.licensedProducts.statusLicensedBoth")
                          : hasXdr
                            ? t("detail.licensedProducts.statusLicensedXdr")
                            : t("detail.licensedProducts.statusLicensedMdr")
                    return (
                      <div className="flex flex-wrap gap-2" aria-label={t("detail.licensedProducts.detectionsSummaryLabel")}>
                        <Badge variant={hasXdr || hasMdr ? "success" : "outline"} size="sm">
                          {t("detail.licensedProducts.detectionsApi", { status: detectionsLabel })}
                        </Badge>
                        <Badge variant={hasMdr ? "success" : "outline"} size="sm">
                          {t("detail.licensedProducts.casesApi", {
                            status: hasMdr
                              ? t("detail.licensedProducts.statusLicensed")
                              : t("detail.licensedProducts.statusNotLicensed"),
                          })}
                        </Badge>
                      </div>
                    )
                  })()}
                  <div className="flex flex-wrap gap-2" aria-label={t("detail.licensedProducts.productsAriaLabel")}>
                    {(overview.licensed_products as LicensedProduct[]).map((product) => {
                      const d = product.details
                      const tooltipParts: string[] = []
                      if (d.quantity != null && !d.unlimited) {
                        tooltipParts.push(
                          d.usageCount != null
                            ? t("detail.licensedProducts.usage", { used: d.usageCount, quantity: d.quantity })
                            : t("detail.licensedProducts.quantity", { quantity: d.quantity }),
                        )
                      }
                      if (d.unlimited) tooltipParts.push(t("detail.licensedProducts.unlimited"))
                      if (d.endDate) tooltipParts.push(t("detail.licensedProducts.validity", { date: d.endDate }))
                      if (d.type) tooltipParts.push(t("detail.licensedProducts.type", { type: d.type }))
                      const tooltip = tooltipParts.length > 0 ? tooltipParts.join(" · ") : undefined
                      const variant: "success" | "warning" | "primary" =
                        product.category === "xdr"
                          ? "success"
                          : product.category === "mdr"
                            ? "warning"
                            : "primary"
                      return (
                        <Badge
                          key={product.code}
                          variant={variant}
                          size="md"
                          title={tooltip}
                          aria-label={tooltip ? `${product.label} — ${tooltip}` : product.label}
                        >
                          {product.label}
                        </Badge>
                      )
                    })}
                  </div>
                </>
              )}
            </Card>
          )}
        </div>
      )}


      {activeTab === "alerts" && !tabLoading && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={() => navigate(buildAlertsPath(integration.id))}>
              {t("detail.viewAllInAlertsRoute")}
            </Button>
            <Button variant="outline" size="sm" onClick={() => navigate(buildAlertsPath(integration.id, { severity: "critical" }))}>
              {t("detail.openCritical")}
            </Button>
            <Button variant="outline" size="sm" onClick={() => navigate(buildAlertsPath(integration.id, { index: "wazuh-archives-*" }))}>
              {t("detail.openArchives")}
            </Button>
          </div>

          {alerts.length === LIST_PAGE_SIZE && (
            <Notice variant="info">
              {t("detail.partialAlertsNotice", { count: LIST_PAGE_SIZE })}
            </Notice>
          )}

          {alerts.length === 0 ? (
            <div className="py-12 text-center text-text-secondary">{t("detail.noAlertsFound")}</div>
          ) : (
            <div className="overflow-x-auto rounded-lg border border-border">
              <table className="w-full min-w-[820px] text-sm" role="table" aria-label={t("detail.alertsTableAriaLabel")}>
                <thead>
                  <tr className="border-b border-border bg-surface-tertiary">
                    <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("detail.columns.severity")}</th>
                    <th scope="col" className={thCls}>{t("detail.columns.title")}</th>
                    <th scope="col" className={thCls}>{t("detail.columns.host")}</th>
                    <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("detail.columns.ruleId")}</th>
                    <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("detail.columns.level")}</th>
                    <th scope="col" className={thCls}>{t("detail.columns.source")}</th>
                    <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("detail.columns.timestamp")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {alerts.map((alert) => (
                    <tr
                      key={alert.alert_id}
                      role="button"
                      tabIndex={0}
                      aria-label={t("detail.viewAlertDetailsAriaLabel", { title: alert.title })}
                      className="cursor-pointer hover:bg-surface-tertiary/50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
                      onClick={() => setSelectedAlert(alert)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault()
                          setSelectedAlert(alert)
                        }
                      }}
                    >
                      <td className={`${tdCls} whitespace-nowrap`}>
                        <Badge variant={SEVERITY_VARIANT[alert.severity] || "default"} size="sm">
                          {alert.severity}
                        </Badge>
                      </td>
                      <td className={tdCls}>
                        <span className="block max-w-[280px] truncate" title={alert.title}>{alert.title}</span>
                      </td>
                      <td className={tdCls}>
                        <span className="block max-w-[180px] truncate" title={alert.hostname || alert.agent_name || undefined}>
                          {alert.hostname || alert.agent_name || "-"}
                        </span>
                      </td>
                      <td className={`${tdCls} whitespace-nowrap font-mono text-xs`}>{alert.rule_id || "-"}</td>
                      <td className={`${tdCls} whitespace-nowrap`}>{alert.rule_level ?? "-"}</td>
                      <td className={`${tdCls} text-xs`}>
                        <span className="block max-w-[180px] truncate" title={alert.src_user || alert.src_ip || alert.decoder_name || undefined}>
                          {alert.src_user || alert.src_ip || alert.decoder_name || "-"}
                        </span>
                      </td>
                      <td className={`${tdCls} whitespace-nowrap text-xs`}>{alert.timestamp ? formatDateTimeIntl(alert.timestamp) : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {activeTab === "health" && !tabLoading && (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-text">{t("detail.health.title")}</h2>
              {healthV2?.last_collection_at && (
                <p className="text-xs text-text-secondary" aria-live="polite">
                  {t("detail.health.lastCollection", { date: formatDateTime(healthV2.last_collection_at) })}
                </p>
              )}
            </div>
          </div>

          <HealthMetricsList
            metrics={healthV2?.metrics ?? []}
            lastCollectionAt={healthV2?.last_collection_at}
            lastSuccessAt={healthV2?.last_success_at}
          />

          {isAdmin && (
            <details className="rounded-2xl border border-border bg-surface shadow-sm">
              <summary className="cursor-pointer list-none px-5 py-4 text-sm font-semibold text-text">
                {t("detail.health.viewRawPayload")}
              </summary>
              <div className="border-t border-border px-5 py-4">
                <pre className="max-h-96 overflow-auto rounded-lg bg-surface-tertiary p-4 font-mono text-xs">
                  {JSON.stringify(health?.details || overview?.health?.details || {}, null, 2)}
                </pre>
              </div>
            </details>
          )}
        </div>
      )}

      {activeTab === "pipeline-health" && (
        <IntegrationHealthPanel integrationId={integrationId} />
      )}

      {activeTab === "destinations" && (
        <IntegrationDestinationsTab integration={integration} />
      )}

      {activeTab === "backfill" && (
        <IntegrationBackfillPanel
          integrationId={integrationId}
          platform={integration.platform}
        />
      )}

      {activeTab === "config" && !tabLoading && isAdmin && (
        <Card padding="md">
          <h3 className="mb-4 font-semibold">{t("detail.config.title")}</h3>
          <dl className="grid grid-cols-1 sm:grid-cols-[180px_1fr] gap-x-4 gap-y-2 text-sm">
            <dt className="font-medium text-text-secondary">{t("detail.config.platform")}</dt>
            <dd>{integration.platform}</dd>
            <dt className="font-medium text-text-secondary">{t("detail.config.organization")}</dt>
            <dd>{integration.organization_name}</dd>
            {integration.platform === "sophos" && (
              <>
                <dt className="font-medium text-text-secondary">{t("detail.config.clientId")}</dt>
                <dd className="font-mono text-xs">{integration.client_id || "-"}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.region")}</dt>
                <dd>{integration.region || "-"}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.tenantId")}</dt>
                <dd className="font-mono text-xs">{integration.tenant_id || "-"}</dd>
              </>
            )}
            {integration.platform === "wazuh" && (
              <>
                <dt className="font-medium text-text-secondary">{t("detail.config.managerUrl")}</dt>
                <dd className="font-mono text-xs">{integration.manager_url || "-"}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.managerUser")}</dt>
                <dd className="font-mono text-xs">{integration.manager_api_username || "-"}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.managerPassword")}</dt>
                <dd>{integration.manager_api_password_configured ? t("detail.config.configured") : t("detail.config.notConfigured")}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.indexerUrl")}</dt>
                <dd className="font-mono text-xs">{integration.indexer_url || "-"}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.indexerUser")}</dt>
                <dd className="font-mono text-xs">{integration.indexer_username || "-"}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.indexerPassword")}</dt>
                <dd>{integration.indexer_password_configured ? t("detail.config.configured") : t("detail.config.notConfigured")}</dd>
                <dt className="font-medium text-text-secondary">{t("detail.config.verifySsl")}</dt>
                <dd>{integration.verify_ssl ? t("detail.config.yes") : t("detail.config.no")}</dd>
              </>
            )}
            <dt className="font-medium text-text-secondary">{t("detail.config.authentication")}</dt>
            <dd>{authStatusLabel(integration.auth_status)}</dd>
            <dt className="font-medium text-text-secondary">{t("detail.config.lastCheck")}</dt>
            <dd>{formatDateTime(integration.last_checked_at)}</dd>
            <dt className="font-medium text-text-secondary">{t("detail.config.lastSuccess")}</dt>
            <dd>{formatDateTime(integration.last_successful_check_at)}</dd>
            <dt className="font-medium text-text-secondary">{t("detail.config.capabilities")}</dt>
            <dd>
              {integration.capabilities.length > 0 ? (
                <span className="flex flex-wrap gap-1">
                  {integration.capabilities.map((cap) => (
                    <Badge key={cap} variant="default" size="sm">
                      {cap}
                    </Badge>
                  ))}
                </span>
              ) : (
                "-"
              )}
            </dd>
          </dl>

          {/* Seção "Capacidades de consulta" — apenas quando há cruzamento */}
          {(() => {
            const matchedCaps = queryCaps.filter((cap) =>
              integration.capabilities.includes(cap.capability)
            )
            if (matchedCaps.length === 0) return null
            return (
              <div className="mt-6">
                <h4 className="mb-3 text-sm font-semibold text-text">{t("detail.config.queryCapabilitiesTitle")}</h4>
                <div className="space-y-3">
                  {matchedCaps.map((cap) => (
                    <div
                      key={cap.dialect}
                      className="rounded-lg border border-border bg-surface-tertiary px-4 py-3 text-sm"
                    >
                      <div className="flex flex-wrap items-center gap-2 mb-2">
                        <Badge variant="primary" size="sm">{cap.dialect}</Badge>
                        {cap.modes.map((mode) => (
                          <Badge key={mode} variant="default" size="sm">{mode}</Badge>
                        ))}
                        {cap.supports_async && (
                          <Badge variant="success" size="sm">async</Badge>
                        )}
                      </div>
                      <dl className="grid grid-cols-1 sm:grid-cols-[160px_1fr] gap-x-3 gap-y-1 text-xs text-text-secondary">
                        {cap.max_window_seconds != null && (
                          <>
                            <dt className="font-medium flex items-center gap-1">
                              {t("detail.config.maxWindow.label")}
                              <HelpTooltip
                                label={t("detail.config.maxWindow.label")}
                                description={t("detail.config.maxWindow.description")}
                                example={`${cap.max_window_seconds}s`}
                              />
                            </dt>
                            <dd>{cap.max_window_seconds}s</dd>
                          </>
                        )}
                        {cap.rate_limit && (
                          <>
                            <dt className="font-medium flex items-center gap-1">
                              {t("detail.config.rateLimit.label")}
                              <HelpTooltip
                                label={t("detail.config.rateLimit.label")}
                                description={t("detail.config.rateLimit.description")}
                                example={cap.rate_limit}
                              />
                            </dt>
                            <dd>{cap.rate_limit}</dd>
                          </>
                        )}
                        {cap.spec_kinds && cap.spec_kinds.length > 0 && (
                          <>
                            <dt className="font-medium flex items-center gap-1">
                              {t("detail.config.specKinds.label")}
                              <HelpTooltip
                                label={t("detail.config.specKinds.label")}
                                description={t("detail.config.specKinds.description")}
                              />
                            </dt>
                            <dd className="flex flex-wrap gap-1">
                              {cap.spec_kinds.map((sk) => (
                                <Badge key={sk} variant="outline" size="sm">{sk}</Badge>
                              ))}
                            </dd>
                          </>
                        )}
                      </dl>
                    </div>
                  ))}
                </div>
              </div>
            )
          })()}
        </Card>
      )}

      <Modal open={editingOpen} onClose={() => setEditingOpen(false)} title={t("detail.editIntegration")} size="xl">
        <IntegrationForm
          mode="edit"
          integration={integration}
          loading={saving}
          onCancel={() => setEditingOpen(false)}
          onSubmit={async (payload) => {
            try {
              setSaving(true)
              setError(null)
              const updated = await api.updateIntegration(integrationId, payload)
              setIntegration(updated)
              setEditingOpen(false)
              if (activeTab !== "config") {
                await loadTabData(activeTab)
              }
            } catch (updateError) {
              const message = updateError instanceof Error ? updateError.message : t("list.feedback.updateError")
              setError(message)
              throw updateError
            } finally {
              setSaving(false)
            }
          }}
        />
      </Modal>

      <AlertDetailsDrawer
        open={!!selectedAlert}
        alert={alertDetail}
        loading={detailLoading}
        error={detailError}
        onClose={() => {
          setSelectedAlert(null)
          setAlertDetail(null)
          setDetailError(null)
        }}
        onPivotRuleId={(ruleId) => navigate(buildAlertsPath(integration.id, { rule_id: ruleId }))}
        onPivotHostname={(hostname) => navigate(buildAlertsPath(integration.id, { hostname }))}
      />
    </div>
  )
}

export default IntegrationDetailPage
