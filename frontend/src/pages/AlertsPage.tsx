"use client"

import type React from "react"
import { useEffect, useMemo, useRef, useState } from "react"
import { useNavigate, useSearchParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  AlertTriangleIcon,
  BellIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  RefreshCwIcon,
  SearchIcon,
  ShieldAlertIcon,
} from "lucide-react"
import * as api from "@/services/api"
import type { Alert, AlertDetail, AlertFilters } from "@/types"
import AlertDetailsDrawer from "@/components/alerts/AlertDetailsDrawer"
import { usePlatform } from "@/contexts/PlatformContext"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import DateRangePicker from "@/components/ui/DateRangePicker/DateRangePicker"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import LoadingSpinner from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import Select, { type SelectValue } from "@/components/ui/Select/Select"
import { DEFAULT_ALERT_INDEX, getAlertDetailFilters, getAlertRequestErrorMessage, normalizeAlertIndex } from "@/lib/alerts"
import { severityLabel, severityVariant } from "@/lib/labels"
import { roundDateToMinute, toUtcZuluString } from "@/lib/utils"
import { formatDateTime } from "@/lib/intl"

type DateRangeValue = {
  from: Date | null
  to: Date | null
}

type AlertFormFilters = {
  index: string
  severity: string
  level: string
  hostname: string
  agent_id: string
  rule_id: string
  rule_group: string
  decoder: string
  src_ip: string
  dst_ip: string
  username: string
  description: string
  description_mode: "smart" | "exact" | "contains"
  query: string
}

type AppliedAlertFilters = AlertFilters

const PAGE_SIZE = 50
const EMPTY_PAGE_META = {
  total: 0,
  limit: PAGE_SIZE,
  offset: 0,
  has_more: false,
}

const DEFAULT_FORM_FILTERS: AlertFormFilters = {
  index: DEFAULT_ALERT_INDEX,
  severity: "",
  level: "",
  hostname: "",
  agent_id: "",
  rule_id: "",
  rule_group: "",
  decoder: "",
  src_ip: "",
  dst_ip: "",
  username: "",
  description: "",
  description_mode: "smart",
  query: "",
}

const QUICK_SEVERITY_VALUES = ["critical", "high", "medium", "low", "info"] as const

const thCls ="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary"
const tdCls = "px-4 py-3 text-sm align-top"

function buildDefaultRange(days: number): DateRangeValue {
  const to = roundDateToMinute(new Date())
  const from = new Date(to.getTime() - days * 24 * 60 * 60 * 1000)
  return { from, to }
}

function buildAppliedFilters(range: DateRangeValue, values: AlertFormFilters): AppliedAlertFilters {
  return {
    index: normalizeAlertIndex(values.index),
    severity: values.severity || undefined,
    level: values.level || undefined,
    hostname: values.hostname || undefined,
    agent_id: values.agent_id || undefined,
    rule_id: values.rule_id || undefined,
    rule_group: values.rule_group || undefined,
    decoder: values.decoder || undefined,
    src_ip: values.src_ip || undefined,
    dst_ip: values.dst_ip || undefined,
    username: values.username || undefined,
    description: values.description || undefined,
    description_mode: values.description_mode,
    query: values.query || undefined,
    time_from: range.from ? toUtcZuluString(range.from) : undefined,
    time_to: range.to ? toUtcZuluString(range.to) : undefined,
  }
}

// Campos de filtro específicos do indexer Wazuh/OpenSearch. Para fontes NÃO-Wazuh (e
// a visão agregada), são removidos da REQUISIÇÃO: um SDPP neutro não envia a taxonomia
// do Wazuh a Sophos/Defender/etc. (que a ignorariam ou rejeitariam). Isto fecha o
// vazamento do `index="wazuh-alerts-*"` default e o "state bleed" ao trocar de fonte.
const WAZUH_ONLY_FILTER_KEYS = [
  "index",
  "level",
  "agent_id",
  "rule_id",
  "rule_group",
  "decoder",
  "src_ip",
  "dst_ip",
  "username",
  "query",
] as const

function scopeFiltersToSource(
  filters: AppliedAlertFilters,
  isWazuhSource: boolean,
): AppliedAlertFilters {
  if (isWazuhSource) return filters
  const scoped: AppliedAlertFilters = { ...filters }
  for (const key of WAZUH_ONLY_FILTER_KEYS) {
    scoped[key] = undefined
  }
  return scoped
}

function hasAdvancedFilters(filters: AlertFormFilters) {
  return Boolean(
    filters.level ||
    filters.agent_id ||
    filters.rule_id ||
    filters.rule_group ||
    filters.decoder ||
    filters.src_ip ||
    filters.dst_ip ||
    filters.username ||
    filters.query,
  )
}

function countAdvancedFilters(filters: AlertFormFilters) {
  return [
    filters.level,
    filters.agent_id,
    filters.rule_id,
    filters.rule_group,
    filters.decoder,
    filters.src_ip,
    filters.dst_ip,
    filters.username,
    filters.query,
  ].filter((value) => value.trim().length > 0).length
}
function parseHighlight(fragment: string, keyPrefix: string) {
  const tokens = fragment.split(/(<em>|<\/em>)/g)
  let highlighted = false

  return tokens.map((token, index) => {
    if (token === "<em>") {
      highlighted = true
      return null
    }
    if (token === "</em>") {
      highlighted = false
      return null
    }
    if (!token) return null
    return highlighted ? (
      <mark key={`${keyPrefix}-${index}`} className="rounded bg-warning-200 px-0.5 text-text">
        {token}
      </mark>
    ) : (
      <span key={`${keyPrefix}-${index}`}>{token}</span>
    )
  })
}

const isAbortError = (cause: unknown) => cause instanceof Error && cause.name === "AbortError"

const AlertsPage: React.FC = () => {
  const { t } = useTranslation("alerts")
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const {
    filteredIntegrations,
    selectedIntegrationId,
    selectedOrgId,
    setSelectedIntegrationId,
  } = usePlatform()

  const defaultRange = useMemo(() => buildDefaultRange(7), [])
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [partialErrors, setPartialErrors] = useState<string[]>([])
  const [dateRange, setDateRange] = useState<DateRangeValue>(defaultRange)
  const [draftFilters, setDraftFilters] = useState<AlertFormFilters>(DEFAULT_FORM_FILTERS)
  const [appliedFilters, setAppliedFilters] = useState<AppliedAlertFilters>(() => buildAppliedFilters(defaultRange, DEFAULT_FORM_FILTERS))
  const [page, setPage] = useState(1)
  const [pageMeta, setPageMeta] = useState(EMPTY_PAGE_META)
  const [refreshToken, setRefreshToken] = useState(0)
  const [isSampled, setIsSampled] = useState(false)
  const [advancedFiltersOpen, setAdvancedFiltersOpen] = useState(false)
  const [selectedAlert, setSelectedAlert] = useState<Alert | null>(null)
  const [alertDetail, setAlertDetail] = useState<AlertDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)

  const requestIdRef = useRef(0)
  const abortControllerRef = useRef<AbortController | null>(null)
  const detailRequestIdRef = useRef(0)
  const detailAbortRef = useRef<AbortController | null>(null)
  const hydratedFromQueryRef = useRef(false)

  const capable = useMemo(
    () => filteredIntegrations.filter((integration) => integration.is_active && integration.capabilities.includes("alerts:list")),
    [filteredIntegrations],
  )

  const selectedTarget = useMemo(() => {
    if (selectedIntegrationId) {
      return capable.find((integration) => integration.id === selectedIntegrationId) ?? null
    }
    return capable.length === 1 ? capable[0] : null
  }, [capable, selectedIntegrationId])

  const targets = useMemo(() => (selectedTarget ? [selectedTarget] : capable), [capable, selectedTarget])
  const isAggregatedView = !selectedTarget && targets.length > 1
  // Filtros de indexer (índice + rule/level/agent/decoder/group/query-string) são
  // específicos de fontes Wazuh/OpenSearch. Um SDPP neutro NÃO impõe a taxonomia do
  // Wazuh a Sophos/Defender/etc. — esses campos só aparecem quando a fonte
  // selecionada é Wazuh (oculto na visão agregada e em fontes não-Wazuh).
  const isWazuhSource = selectedTarget?.platform === "wazuh"
  const totalPages = Math.max(1, Math.ceil(pageMeta.total / Math.max(pageMeta.limit || PAGE_SIZE, 1)))
  const visibleStart = alerts.length === 0 ? 0 : pageMeta.offset + 1
  const visibleEnd = pageMeta.offset + alerts.length

  useEffect(() => {
    if (hydratedFromQueryRef.current) return

    const integrationParam = searchParams.get("integration_id")
    if (integrationParam) {
      const integrationId = Number(integrationParam)
      if (!Number.isNaN(integrationId)) {
        setSelectedIntegrationId(integrationId)
      }
    }

    const seededRange = buildDefaultRange(Number(searchParams.get("days") || 7))
    const timeFromParam = searchParams.get("time_from")
    const timeToParam = searchParams.get("time_to")
    if (timeFromParam || timeToParam) {
      seededRange.from = timeFromParam ? new Date(timeFromParam) : null
      seededRange.to = timeToParam ? new Date(timeToParam) : null
    }
    const seededFilters: AlertFormFilters = {
      ...DEFAULT_FORM_FILTERS,
      index: searchParams.get("index") || DEFAULT_FORM_FILTERS.index,
      severity: searchParams.get("severity") || "",
      level: searchParams.get("level") || "",
      hostname: searchParams.get("hostname") || "",
      agent_id: searchParams.get("agent_id") || "",
      rule_id: searchParams.get("rule_id") || "",
      rule_group: searchParams.get("rule_group") || "",
      decoder: searchParams.get("decoder") || "",
      src_ip: searchParams.get("src_ip") || "",
      dst_ip: searchParams.get("dst_ip") || "",
      username: searchParams.get("username") || "",
      description: searchParams.get("description") || "",
      description_mode: (searchParams.get("description_mode") as AlertFormFilters["description_mode"]) || "smart",
      query: searchParams.get("query") || "",
    }

    const hasSeededFilters = Object.entries(seededFilters).some(([key, value]) => key !== "index" && value !== "" && value !== "smart")
      || seededFilters.index !== DEFAULT_ALERT_INDEX
      || Boolean(searchParams.get("integration_id"))

    if (hasSeededFilters) {
      setDateRange(seededRange)
      setDraftFilters(seededFilters)
      setAppliedFilters(buildAppliedFilters(seededRange, seededFilters))
      setAdvancedFiltersOpen(hasAdvancedFilters(seededFilters))
    }

    hydratedFromQueryRef.current = true
  }, [searchParams, setSelectedIntegrationId])

  useEffect(() => {
    setPage(1)
  }, [selectedTarget?.id])

  // Ao trocar p/ uma fonte não-Wazuh (ou visão agregada), limpa os filtros de indexer
  // do estado visível + da URL. A requisição já é escopada em loadAlerts (safety net);
  // este efeito alinha formulário/URL e evita o "state bleed" entre fontes.
  useEffect(() => {
    if (isWazuhSource) return
    setDraftFilters((current) => ({
      ...current,
      index: DEFAULT_FORM_FILTERS.index,
      level: "",
      agent_id: "",
      rule_id: "",
      rule_group: "",
      decoder: "",
      src_ip: "",
      dst_ip: "",
      username: "",
      query: "",
    }))
    setAppliedFilters((current) => scopeFiltersToSource(current, false))
  }, [isWazuhSource])

  useEffect(() => {
    abortControllerRef.current?.abort()
    const controller = new AbortController()
    abortControllerRef.current = controller
    const requestId = requestIdRef.current + 1
    requestIdRef.current = requestId

    const loadAlerts = async () => {
      if (targets.length === 0) {
        setAlerts([])
        setPartialErrors([])
        setError(null)
        setPageMeta(EMPTY_PAGE_META)
        setIsSampled(false)
        setLoading(false)
        return
      }

      setLoading(true)
      setError(null)
      setPartialErrors([])
      setIsSampled(false)

      try {
        const offset = (page - 1) * PAGE_SIZE

        if (selectedTarget) {
          const data = await api.listAlerts(
            selectedTarget.id,
            {
              limit: PAGE_SIZE,
              offset,
              ...scopeFiltersToSource(appliedFilters, isWazuhSource),
            },
            { signal: controller.signal },
          )

          if (controller.signal.aborted || requestId !== requestIdRef.current) {
            return
          }

          setAlerts(data.items)
          setPageMeta({
            total: data.total,
            limit: data.limit || PAGE_SIZE,
            offset: data.offset ?? offset,
            has_more: data.has_more,
          })
          return
        }

        const data = await api.listAggregatedAlerts(
          {
            organization_id: selectedOrgId,
            integration_ids: targets.map((integration) => integration.id),
            limit: PAGE_SIZE,
            offset,
            // Visão agregada cruza fontes mistas → sem selectedTarget, isWazuhSource é
            // false e os filtros de indexer são removidos (universais permanecem).
            ...scopeFiltersToSource(appliedFilters, isWazuhSource),
          },
          { signal: controller.signal },
        )

        if (controller.signal.aborted || requestId !== requestIdRef.current) {
          return
        }

        setAlerts(data.items)
        setPartialErrors(data.partial_errors)
        setPageMeta({
          total: data.total,
          limit: data.limit || PAGE_SIZE,
          offset: data.offset ?? offset,
          has_more: data.has_more,
        })
        setIsSampled(data.is_sampled)
      } catch (cause) {
        if (isAbortError(cause) || requestId !== requestIdRef.current) {
          return
        }

        setAlerts([])
        setPageMeta(EMPTY_PAGE_META)
        setPartialErrors([])
        setIsSampled(false)
        setError(getAlertRequestErrorMessage(cause, t("errors.queryFailed")))
      } finally {
        if (!controller.signal.aborted && requestId === requestIdRef.current) {
          setLoading(false)
        }
      }
    }

    void loadAlerts()

    return () => {
      controller.abort()
    }
  }, [appliedFilters, isWazuhSource, page, refreshToken, selectedOrgId, selectedTarget, targets, t])

  useEffect(() => {
    if (!selectedAlert?.alert_id || !selectedAlert.integration_id) {
      setAlertDetail(selectedAlert)
      setDetailError(null)
      setDetailLoading(false)
      return
    }

    detailAbortRef.current?.abort()
    const controller = new AbortController()
    detailAbortRef.current = controller
    const requestId = detailRequestIdRef.current + 1
    detailRequestIdRef.current = requestId

    const loadDetail = async () => {
      setDetailLoading(true)
      setDetailError(null)
      setAlertDetail(selectedAlert)

      try {
        const detail = await api.getAlertDetail(
          selectedAlert.integration_id,
          selectedAlert.alert_id,
          getAlertDetailFilters(selectedAlert, appliedFilters),
          { signal: controller.signal },
        )

        if (controller.signal.aborted || requestId !== detailRequestIdRef.current) {
          return
        }

        setAlertDetail(detail)
      } catch (cause) {
        if (isAbortError(cause) || requestId !== detailRequestIdRef.current) {
          return
        }
        setDetailError(getAlertRequestErrorMessage(cause, t("errors.detailLoadFailed")))
        setAlertDetail(selectedAlert)
      } finally {
        if (!controller.signal.aborted && requestId === detailRequestIdRef.current) {
          setDetailLoading(false)
        }
      }
    }

    void loadDetail()

    return () => {
      controller.abort()
    }
  }, [appliedFilters.index, selectedAlert, t])

  const stats = useMemo(() => {
    return alerts.reduce(
      (acc, alert) => {
        acc.total += 1
        if (alert.severity in acc) {
          acc[alert.severity as keyof typeof acc] += 1
        }
        return acc
      },
      { total: 0, critical: 0, high: 0, medium: 0, low: 0, info: 0 },
    )
  }, [alerts])

  const handleSearch = (event: React.FormEvent) => {
    event.preventDefault()
    setPage(1)
    setAppliedFilters(buildAppliedFilters(dateRange, draftFilters))
    setRefreshToken((current) => current + 1)
  }

  const handleReset = () => {
    const range = buildDefaultRange(7)
    setDateRange(range)
    setDraftFilters(DEFAULT_FORM_FILTERS)
    setAdvancedFiltersOpen(false)
    setPage(1)
    setAppliedFilters(buildAppliedFilters(range, DEFAULT_FORM_FILTERS))
    setRefreshToken((current) => current + 1)
  }

  const handleRefresh = () => {
    setRefreshToken((current) => current + 1)
  }

  const handleQuickSeverity = (severity: string) => {
    const nextFilters = { ...draftFilters, severity }
    setDraftFilters(nextFilters)
    setPage(1)
    setAppliedFilters(buildAppliedFilters(dateRange, nextFilters))
    setRefreshToken((current) => current + 1)
  }

  const handleOpenDetails = (alert: Alert) => {
    setSelectedAlert(alert)
    setAlertDetail(alert)
  }

  const handlePivotRuleId = (ruleId: string) => {
    // rule_id é específico do indexer Wazuh; o pivot é no-op em fontes não-Wazuh
    // (o campo está oculto, não deve poluir a consulta silenciosamente).
    if (!isWazuhSource) return
    const nextFilters = { ...draftFilters, rule_id: ruleId }
    setDraftFilters(nextFilters)
    setAdvancedFiltersOpen(true)
    setPage(1)
    setAppliedFilters(buildAppliedFilters(dateRange, nextFilters))
    setRefreshToken((current) => current + 1)
  }

  const handlePivotHostname = (hostname: string) => {
    const nextFilters = { ...draftFilters, hostname }
    setDraftFilters(nextFilters)
    setPage(1)
    setAppliedFilters(buildAppliedFilters(dateRange, nextFilters))
    setRefreshToken((current) => current + 1)
  }


  return (
    <div className="space-y-6">
      <PageHeader
        eyebrow={t("page.eyebrow")}
        icon={<BellIcon size={24} />}
        title={t("page.title")}
        description={t("page.description")}
        actions={
          <Button variant="outline" size="sm" onClick={handleRefresh} disabled={loading} leftIcon={<RefreshCwIcon size={14} />}>
            {t("common:actions.refresh")}
          </Button>
        }
      />

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-5">
        {[
          { label: t("stats.criticalShown"), value: stats.critical },
          { label: t("stats.highShown"), value: stats.high },
          { label: t("stats.mediumShown"), value: stats.medium },
          { label: t("stats.lowShown"), value: stats.low },
          { label: t("stats.infoShown"), value: stats.info },
        ].map((item) => (
          <Card key={item.label} padding="sm" className="shadow-sm">
            <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{item.label}</div>
            <div className="mt-2 text-2xl font-bold text-text">{item.value}</div>
          </Card>
        ))}
      </div>

      <Card className="shadow-sm">
        <form onSubmit={handleSearch} className="space-y-5 p-5">
          <div className="flex flex-wrap gap-2">
            {QUICK_SEVERITY_VALUES.map((value) => (
              <Button
                key={value}
                type="button"
                size="sm"
                variant={draftFilters.severity === value ? "primary" : "outline"}
                onClick={() => handleQuickSeverity(draftFilters.severity === value ? "" : value)}
              >
                {t(`filters.quickSeverity.${value}`)}
              </Button>
            ))}
          </div>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_220px_220px_220px]">
            <DateRangePicker label={t("filters.period")} value={dateRange} onChange={setDateRange} />

            <Select
              label={t("filters.severity")}
              placeholder={t("common:states.all")}
              options={[
                { value: "", label: t("common:states.all") },
                { value: "critical", label: severityLabel("critical") },
                { value: "high", label: severityLabel("high") },
                { value: "medium", label: severityLabel("medium") },
                { value: "low", label: severityLabel("low") },
                { value: "info", label: severityLabel("info") },
              ]}
              value={draftFilters.severity}
              onValueChange={(value: SelectValue) =>
                setDraftFilters((current) => ({ ...current, severity: String(Array.isArray(value) ? value[0] ?? "" : value) }))
              }
            />

            <Input
              label={t("filters.hostname")}
              value={draftFilters.hostname}
              onChange={(event) => setDraftFilters((current) => ({ ...current, hostname: event.target.value }))}
              placeholder={t("filters.hostnamePlaceholder")}
              leftIcon={<SearchIcon size={16} />}
            />

            {isWazuhSource && (
              <Select
                label={t("filters.index")}
                options={[
                  { value: "wazuh-alerts-*", label: "wazuh-alerts-*" },
                  { value: "wazuh-archives-*", label: "wazuh-archives-*" },
                ]}
                value={draftFilters.index}
                onValueChange={(value: SelectValue) =>
                  setDraftFilters((current) => ({ ...current, index: String(Array.isArray(value) ? value[0] ?? "" : value) }))
                }
              />
            )}
          </div>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_220px]">
            <Input
              label={t("filters.description")}
              value={draftFilters.description}
              onChange={(event) => setDraftFilters((current) => ({ ...current, description: event.target.value }))}
              placeholder={t("filters.descriptionPlaceholder")}
              helperText={t("filters.descriptionHelper")}
            />
            <Select
              label={t("filters.descriptionMode")}
              options={[
                { value: "smart", label: t("filters.descriptionModeOptions.smart") },
                { value: "exact", label: t("filters.descriptionModeOptions.exact") },
                { value: "contains", label: t("filters.descriptionModeOptions.contains") },
              ]}
              value={draftFilters.description_mode}
              onValueChange={(value: SelectValue) =>
                setDraftFilters((current) => ({
                  ...current,
                  description_mode: String(Array.isArray(value) ? value[0] ?? "smart" : value) as AlertFormFilters["description_mode"],
                }))
              }
            />
          </div>

          {isWazuhSource && (
          <div className="overflow-hidden rounded-2xl border border-border bg-surface-tertiary/30">
            <button
              type="button"
              className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left"
              onClick={() => setAdvancedFiltersOpen((current) => !current)}
              aria-expanded={advancedFiltersOpen}
            >
              <div>
                <div className="text-sm font-semibold text-text">{t("filters.advanced.title")}</div>
                <div className="text-xs text-text-secondary">
                  {countAdvancedFilters(draftFilters) > 0
                    ? t("filters.advanced.filledCount", { count: countAdvancedFilters(draftFilters) })
                    : t("filters.advanced.hint")}
                </div>
              </div>
              {advancedFiltersOpen ? <ChevronUpIcon size={16} className="text-text-tertiary" /> : <ChevronDownIcon size={16} className="text-text-tertiary" />}
            </button>

            {advancedFiltersOpen && (
              <div className="space-y-4 border-t border-border px-4 py-4">
                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
                  <Input
                    label={t("filters.advanced.ruleId")}
                    value={draftFilters.rule_id}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, rule_id: event.target.value }))}
                    placeholder={t("filters.advanced.ruleIdPlaceholder")}
                  />
                  <Input
                    label={t("filters.advanced.level")}
                    value={draftFilters.level}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, level: event.target.value }))}
                    placeholder={t("filters.advanced.levelPlaceholder")}
                  />
                  <Input
                    label={t("filters.advanced.agentId")}
                    value={draftFilters.agent_id}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, agent_id: event.target.value }))}
                    placeholder={t("filters.advanced.agentIdPlaceholder")}
                  />
                  <Input
                    label={t("filters.advanced.decoder")}
                    value={draftFilters.decoder}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, decoder: event.target.value }))}
                    placeholder={t("filters.advanced.decoderPlaceholder")}
                  />
                </div>

                <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
                  <Input
                    label={t("filters.advanced.ruleGroup")}
                    value={draftFilters.rule_group}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, rule_group: event.target.value }))}
                    placeholder={t("filters.advanced.ruleGroupPlaceholder")}
                  />
                  <Input
                    label={t("filters.advanced.srcIp")}
                    value={draftFilters.src_ip}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, src_ip: event.target.value }))}
                    placeholder={t("filters.advanced.srcIpPlaceholder")}
                  />
                  <Input
                    label={t("filters.advanced.dstIp")}
                    value={draftFilters.dst_ip}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, dst_ip: event.target.value }))}
                    placeholder={t("filters.advanced.dstIpPlaceholder")}
                  />
                  <Input
                    label={t("filters.advanced.username")}
                    value={draftFilters.username}
                    onChange={(event) => setDraftFilters((current) => ({ ...current, username: event.target.value }))}
                    placeholder={t("filters.advanced.usernamePlaceholder")}
                  />
                </div>

                <Input
                  label={t("filters.advanced.query")}
                  value={draftFilters.query}
                  onChange={(event) => setDraftFilters((current) => ({ ...current, query: event.target.value }))}
                  placeholder={t("filters.advanced.queryPlaceholder")}
                  helperText={t("filters.advanced.queryHelper")}
                />
              </div>
            )}
          </div>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button type="submit" variant="primary" loading={loading} disabled={loading}>
              {t("filters.apply")}
            </Button>
            <Button type="button" variant="outline" onClick={handleReset} disabled={loading}>
              {t("common:actions.clear")}
            </Button>
            <div className="text-sm text-text-secondary">
              {targets.length === 0
                ? t("results.noIntegration")
                : selectedTarget
                  ? t("results.showingScoped", { start: visibleStart, end: visibleEnd, total: pageMeta.total, name: selectedTarget.name })
                  : t("results.showingFederated", { start: visibleStart, end: visibleEnd, total: pageMeta.total })}
            </div>
          </div>
        </form>
      </Card>

      {isAggregatedView && (
        <Notice variant="info" title={t("notices.federatedMode.title")}>
          {t("notices.federatedMode.description")}
        </Notice>
      )}

      {isSampled && (
        <Notice variant="warning" title={t("notices.partialResult.title")}>
          {t("notices.partialResult.description")}
        </Notice>
      )}

      {error && (
        <Notice variant="danger" title={t("notices.loadFailed.title")}>
          {error}
        </Notice>
      )}

      {partialErrors.length > 0 && (
        <Notice variant="warning" title={t("notices.sourcesNotResponding.title")}>
          {partialErrors.join(" | ")}
        </Notice>
      )}

      {capable.length === 0 && (
        <EmptyState
          icon={<ShieldAlertIcon size={48} />}
          title={t("emptyStates.noIntegration.title")}
          description={t("emptyStates.noIntegration.description")}
        />
      )}

      {loading && (
        <div className="flex min-h-[280px] items-center justify-center py-12" aria-busy="true">
          <LoadingSpinner text={t("loading")} />
        </div>
      )}

      {!loading && capable.length > 0 && alerts.length === 0 && !error && (
        <Card padding="lg" className="text-center text-sm text-text-secondary shadow-sm">
          {t("emptyStates.noResults")}
        </Card>
      )}

      {!loading && capable.length > 0 && alerts.length > 0 && (
        <div className="overflow-hidden rounded-2xl border border-border bg-surface shadow-sm">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[960px] text-sm" role="table" aria-label={t("table.ariaLabel")}>
              <thead>
                <tr className="border-b border-border bg-surface-tertiary">
                  <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("table.columns.date")}</th>
                  <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("table.columns.severity")}</th>
                  <th scope="col" className={thCls}>{t("table.columns.description")}</th>
                  <th scope="col" className={thCls}>{t("table.columns.host")}</th>
                  <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("table.columns.rule")}</th>
                  <th scope="col" className={`${thCls} whitespace-nowrap`}>{t("table.columns.level")}</th>
                  <th scope="col" className={thCls}>{t("table.columns.origin")}</th>
                  <th scope="col" className={thCls}>{t("table.columns.integration")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {alerts.map((alert) => {
                  const descriptionHighlights = alert.highlights["rule.description"] ?? []
                  const summarySnippet = descriptionHighlights[0]
                  return (
                    <tr
                      key={alert.alert_id}
                      className="cursor-pointer hover:bg-surface-tertiary/50 focus-visible:bg-surface-tertiary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary-500/40"
                      onClick={() => handleOpenDetails(alert)}
                      role="button"
                      tabIndex={0}
                      aria-label={t("table.viewDetailsAria", { title: alert.title || alert.alert_id })}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault()
                          handleOpenDetails(alert)
                        }
                      }}
                    >
                      <td className={`${tdCls} whitespace-nowrap text-xs`}>{alert.timestamp ? formatDateTime(alert.timestamp, { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "-"}</td>
                      <td className={tdCls}>
                        <Badge variant={severityVariant(alert.severity)} size="sm">
                          {severityLabel(alert.severity)}
                        </Badge>
                      </td>
                      <td className={`${tdCls} max-w-[440px]`}>
                        <div className="flex items-start gap-2">
                          <AlertTriangleIcon size={14} className="mt-0.5 shrink-0 text-text-tertiary" />
                          <div className="space-y-1">
                            <div className="line-clamp-2 text-text">{alert.title || "-"}</div>
                            {summarySnippet && (
                              <div className="line-clamp-2 text-xs text-text-secondary">
                                {parseHighlight(summarySnippet, `${alert.alert_id}-highlight`)}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className={tdCls}>
                        {(() => {
                          const host = alert.hostname || alert.agent_name || "-"
                          return (
                            <span className="block max-w-[180px] truncate" title={host}>
                              {host}
                            </span>
                          )
                        })()}
                      </td>
                      <td className={`${tdCls} whitespace-nowrap font-mono text-xs`}>{alert.rule_id || "-"}</td>
                      <td className={`${tdCls} whitespace-nowrap`}>{alert.rule_level ?? "-"}</td>
                      <td className={`${tdCls} text-xs`}>
                        {(() => {
                          const origin = alert.src_user || alert.src_ip || alert.decoder_name || "-"
                          return (
                            <span className="block max-w-[160px] truncate" title={origin}>
                              {origin}
                            </span>
                          )
                        })()}
                      </td>
                      <td className={`${tdCls} text-xs`}>
                        {(() => {
                          const integration = alert.integration_name || selectedTarget?.name || "-"
                          return (
                            <span className="block max-w-[160px] truncate" title={integration}>
                              {integration}
                            </span>
                          )
                        })()}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>

          <div className="flex flex-col gap-3 border-t border-border px-4 py-3 text-sm text-text-secondary sm:flex-row sm:items-center sm:justify-between">
            <div>
              {t("pagination.summary", { page, totalPages, start: visibleStart, end: visibleEnd, total: pageMeta.total })}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => setPage((current) => Math.max(1, current - 1))} disabled={loading || page <= 1}>
                {t("pagination.previous")}
              </Button>
              <div className="min-w-24 text-center text-xs uppercase tracking-wider text-text-tertiary">
                {t("pagination.perPage", { count: pageMeta.limit || PAGE_SIZE })}
              </div>
              <Button variant="outline" size="sm" onClick={() => setPage((current) => current + 1)} disabled={loading || !pageMeta.has_more}>
                {t("pagination.next")}
              </Button>
            </div>
          </div>
        </div>
      )}

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
        onPivotRuleId={handlePivotRuleId}
        onPivotHostname={handlePivotHostname}
      />
    </div>
  )
}

export default AlertsPage
