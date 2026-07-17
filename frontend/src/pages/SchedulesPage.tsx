"use client"

import type React from "react"
import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  BarChart3Icon,
  BellIcon,
  Building2Icon,
  CalendarIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  DownloadIcon,
  EyeIcon,
  RefreshCcwIcon,
  SearchIcon,
  Trash2Icon,
} from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/Card/Card"
import { Button } from "@/components/ui/Button/Button"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import EmptyState from "@/components/ui/EmptyState/EmptyState"
import { Input } from "@/components/ui/Input/Input"
import LoadingSpinner from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import Select, { type SelectValue } from "@/components/ui/Select/Select"
import { useClients } from "@/hooks/useClients"
import { useForm } from "@/hooks/useForm"
import { useQueries } from "@/hooks/useQueries"
import * as api from "@/services/api"
import { formatDateTime as formatDateTimeIntl } from "@/lib/intl"
import type { Schedule, ScheduleTimeUnit, SearchHistoryItem } from "@/types"

type Feedback =
  | {
      type: "success" | "error"
      message: string
    }
  | null

interface ScheduleFormValues {
  query_id: number | ""
  client_ids: number[]
  interval_value: number | ""
  interval_unit: ScheduleTimeUnit
  lookback_value: number | ""
  lookback_unit: ScheduleTimeUnit
  notify_on_results: boolean
}

type TFunc = (key: string, options?: Record<string, unknown>) => string

function getScheduleUnitLabels(t: TFunc): Record<ScheduleTimeUnit, [string, string]> {
  return {
    minutes: [t("schedules:units.minute"), t("schedules:units.minutes")],
    hours: [t("schedules:units.hour"), t("schedules:units.hours")],
    days: [t("schedules:units.day"), t("schedules:units.days")],
    weeks: [t("schedules:units.week"), t("schedules:units.weeks")],
  }
}

function getScheduleUnitOptions(t: TFunc) {
  return [
    { value: "minutes", label: t("schedules:units.minutesLabel") },
    { value: "hours", label: t("schedules:units.hoursLabel") },
    { value: "days", label: t("schedules:units.daysLabel") },
    { value: "weeks", label: t("schedules:units.weeksLabel") },
  ] as const
}

const initialFormValues: ScheduleFormValues = {
  query_id: "",
  client_ids: [],
  interval_value: 1,
  interval_unit: "hours",
  lookback_value: 1,
  lookback_unit: "days",
  notify_on_results: false,
}

const thCls = "px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary"
const tdCls = "px-4 py-4 align-top text-sm text-text"

/** Quantos clientes mostrar inline antes de colapsar em "+N". */
const MAX_VISIBLE_CLIENTS = 2
/** Linhas por página no histórico — evita que a tabela cresça sem limite. */
const HISTORY_PAGE_SIZE = 8

type HistoryStatusFilter = "all" | "success" | "error" | "running"

function getHistoryStatusFilters(t: TFunc): { value: HistoryStatusFilter; label: string }[] {
  return [
    { value: "all", label: t("schedules:history.filters.all") },
    { value: "success", label: t("schedules:history.filters.success") },
    { value: "error", label: t("schedules:history.filters.error") },
    { value: "running", label: t("schedules:history.filters.running") },
  ]
}

/** Rótulo amigável (localizado) para o status cru vindo do backend. */
function getStatusLabel(status: string, t: TFunc) {
  const normalized = status.trim().toLowerCase()
  if (normalized === "finished" || normalized === "completed") return t("schedules:history.status.completed")
  if (normalized === "failed") return t("schedules:history.status.failed")
  if (normalized === "cancelled") return t("schedules:history.status.cancelled")
  if (normalized === "running" || normalized === "pending" || normalized === "queued") return t("schedules:history.status.running")
  return status
}

function normalizeSelectValues(value: SelectValue): number[] {
  const values = Array.isArray(value) ? value : [value]

  return values
    .map((item) => Number(item))
    .filter((item) => !Number.isNaN(item) && item > 0)
}

function parseUtcDate(dateString: string) {
  const normalized = /(?:[zZ]|[+-]\d{2}:\d{2})$/.test(dateString) ? dateString : `${dateString}Z`
  return new Date(normalized)
}

function formatDateTime(dateString: string | null | undefined, t: TFunc) {
  if (!dateString) {
    return t("schedules:history.notExecuted")
  }

  const parsedDate = parseUtcDate(dateString)
  return Number.isNaN(parsedDate.getTime()) ? dateString : formatDateTimeIntl(parsedDate)
}

function formatDuration(value: number, unit: ScheduleTimeUnit, t: TFunc) {
  const [singular, plural] = getScheduleUnitLabels(t)[unit]
  return `${value} ${value === 1 ? singular : plural}`
}

function getHistoryResultCount(historyItem: SearchHistoryItem) {
  if (typeof historyItem.result_count === "number") {
    return historyItem.result_count
  }

  if (!historyItem.result_json?.trim()) {
    return historyItem.error_message ? 0 : null
  }

  try {
    const parsed = JSON.parse(historyItem.result_json)
    const items = parsed?.items || parsed?.results || []
    return Array.isArray(items) ? items.length : 0
  } catch {
    return null
  }
}

function formatResultPayload(historyItem: SearchHistoryItem, t: TFunc) {
  if (!historyItem.result_json?.trim()) {
    return historyItem.error_message || t("schedules:previewModal.noResultPersisted")
  }

  try {
    return JSON.stringify(JSON.parse(historyItem.result_json), null, 2)
  } catch {
    return historyItem.result_json
  }
}

function isSuccessfulStatus(status: string) {
  const normalized = status.trim().toLowerCase()
  return normalized === "finished" || normalized === "completed"
}

function isErrorStatus(status: string) {
  const normalized = status.trim().toLowerCase()
  return normalized === "failed" || normalized === "cancelled"
}

function getStatusClassName(status: string) {
  if (isSuccessfulStatus(status)) {
    return "inline-flex items-center rounded-full bg-success-50 px-2.5 py-0.5 text-xs font-medium text-success-700"
  }
  if (isErrorStatus(status)) {
    return "inline-flex items-center rounded-full bg-danger-50 px-2.5 py-0.5 text-xs font-medium text-danger-700"
  }
  return "inline-flex items-center rounded-full bg-warning-50 px-2.5 py-0.5 text-xs font-medium text-warning-700"
}

export const SchedulesPage: React.FC = () => {
  const { t } = useTranslation("schedules")
  const { queries, loading: queriesLoading, error: queriesError, refetch: refetchQueries } = useQueries()
  const { clients, loading: clientsLoading, error: clientsError, refetch: refetchClients } = useClients()

  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [selectedScheduleId, setSelectedScheduleId] = useState<number | null>(null)
  const [historyItems, setHistoryItems] = useState<SearchHistoryItem[]>([])
  const [schedulesLoading, setSchedulesLoading] = useState(true)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState<string | null>(null)
  const [notificationRecipientsCount, setNotificationRecipientsCount] = useState(0)
  const [feedback, setFeedback] = useState<Feedback>(null)
  const [deleteCandidate, setDeleteCandidate] = useState<Schedule | null>(null)
  const [deletingScheduleId, setDeletingScheduleId] = useState<number | null>(null)
  const [previewItem, setPreviewItem] = useState<SearchHistoryItem | null>(null)
  const [historyRequestVersion, setHistoryRequestVersion] = useState(0)
  const [historyStatusFilter, setHistoryStatusFilter] = useState<HistoryStatusFilter>("all")
  const [historySearch, setHistorySearch] = useState("")
  const [historyPage, setHistoryPage] = useState(1)
  const historySectionRef = useRef<HTMLDivElement | null>(null)

  const selectedSchedule = schedules.find((schedule) => schedule.id === selectedScheduleId) || null

  function getClientName(clientId?: number | null) {
    if (clientId === undefined || clientId === null) return null
    const client = clients.find((item) => item.id === clientId)
    return client?.name || t("schedules:clientFallback", { id: clientId })
  }
  const availableClients = clients.filter((client) => client.is_authenticated && Boolean(client.tenant_id))
  const scheduleUnitOptions = useMemo(() => getScheduleUnitOptions(t), [t])
  const historyStatusFilters = useMemo(() => getHistoryStatusFilters(t), [t])

  const {
    values: scheduleValues,
    errors: scheduleErrors,
    handleChange: handleScheduleChange,
    handleSubmit: handleScheduleSubmit,
    isSubmitting: isScheduleSubmitting,
    resetForm: resetScheduleForm,
    setFieldError: setScheduleFieldError,
    setFieldValue: setScheduleFieldValue,
  } = useForm<ScheduleFormValues>({
    initialValues: initialFormValues,
    validate: (values) => {
      const errors: Partial<Record<keyof ScheduleFormValues, string>> = {}

      if (!values.query_id) {
        errors.query_id = t("schedules:form.validation.queryRequired")
      }

      if (!values.client_ids.length) {
        errors.client_ids = t("schedules:form.validation.clientsRequired")
      }

      if (!values.interval_value || Number(values.interval_value) <= 0) {
        errors.interval_value = t("schedules:form.validation.intervalInvalid")
      }

      if (!values.lookback_value || Number(values.lookback_value) <= 0) {
        errors.lookback_value = t("schedules:form.validation.lookbackInvalid")
      }

      return errors
    },
    onSubmit: async (values) => {
      try {
        setFeedback(null)

        await api.createSchedule({
          query_id: Number(values.query_id),
          client_ids: values.client_ids,
          interval_value: Number(values.interval_value),
          interval_unit: values.interval_unit,
          lookback_value: Number(values.lookback_value),
          lookback_unit: values.lookback_unit,
          notify_on_results: values.notify_on_results,
        })

        setFeedback({ type: "success", message: t("schedules:feedback.createSuccess") })
        resetScheduleForm()
        await refreshSchedules()
      } catch (error) {
        const message = error instanceof Error ? error.message : t("schedules:feedback.createError")
        setFeedback({ type: "error", message })
      }
    },
  })

  function getValidClientIds(candidateIds: number[]) {
    const availableClientIds = new Set(availableClients.map((client) => client.id))
    return candidateIds.filter((clientId) => availableClientIds.has(clientId))
  }

  async function refreshSchedules(preferredScheduleId?: number | null) {
    try {
      setSchedulesLoading(true)
      const nextSchedules = await api.listSchedules()
      setSchedules(nextSchedules)
      setSelectedScheduleId((currentSelectedId) => {
        const candidateId = typeof preferredScheduleId === "number" ? preferredScheduleId : currentSelectedId

        if (typeof candidateId === "number" && nextSchedules.some((schedule) => schedule.id === candidateId)) {
          return candidateId
        }

        return nextSchedules[0]?.id ?? null
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : t("schedules:feedback.loadSchedulesError")
      setFeedback({ type: "error", message })
    } finally {
      setSchedulesLoading(false)
    }
  }

  async function refreshNotificationRecipients() {
    try {
      const recipients = await api.listEmails()
      setNotificationRecipientsCount(recipients.length)
    } catch (error) {
      const message = error instanceof Error ? error.message : t("schedules:feedback.loadEmailsError")
      setFeedback({ type: "error", message })
    }
  }

  async function handleRefresh() {
    setFeedback(null)

    try {
      await Promise.all([
        refreshSchedules(selectedScheduleId),
        refreshNotificationRecipients(),
        refetchQueries(),
        refetchClients(),
      ])
    } catch (error) {
      const message = error instanceof Error ? error.message : t("schedules:feedback.refreshError")
      setFeedback({ type: "error", message })
    }
  }

  function handleScheduleQueryChange(value: SelectValue) {
    const nextQueryId = Array.isArray(value) ? Number(value[0]) : Number(value)

    if (!nextQueryId || Number.isNaN(nextQueryId)) {
      setScheduleFieldValue("query_id", "")
      return
    }

    setScheduleFieldValue("query_id", nextQueryId)
    setScheduleFieldError("query_id", "")

    const selectedQuery = queries.find((query) => query.id === nextQueryId)
    if (selectedQuery?.client_ids?.length) {
      setScheduleFieldValue("client_ids", getValidClientIds(selectedQuery.client_ids))
      setScheduleFieldError("client_ids", "")
    }
  }

  function handleClientSelectionChange(value: SelectValue) {
    const nextClientIds = normalizeSelectValues(value)
    setScheduleFieldValue("client_ids", nextClientIds)
    if (nextClientIds.length > 0) {
      setScheduleFieldError("client_ids", "")
    }
  }

  function handleUnitSelectionChange(field: "interval_unit" | "lookback_unit", value: SelectValue) {
    const resolvedValue = Array.isArray(value) ? String(value[0] || "") : String(value)
    if (resolvedValue) {
      setScheduleFieldValue(field, resolvedValue as ScheduleTimeUnit)
    }
  }

  async function confirmDeleteSchedule() {
    if (!deleteCandidate) {
      return
    }

    try {
      setFeedback(null)
      setDeletingScheduleId(deleteCandidate.id)
      await api.deleteSchedule(deleteCandidate.id)
      setFeedback({ type: "success", message: t("schedules:feedback.deleteSuccess") })
      setDeleteCandidate(null)
      setPreviewItem(null)
      await refreshSchedules()
    } catch (error) {
      const message = error instanceof Error ? error.message : t("schedules:feedback.deleteError")
      setFeedback({ type: "error", message })
    } finally {
      setDeletingScheduleId(null)
    }
  }

  async function handleDownloadResult(historyItem: SearchHistoryItem) {
    try {
      setFeedback(null)
      await api.downloadStoredCSV(historyItem.search_id)
    } catch (error) {
      const message = error instanceof Error ? error.message : t("schedules:feedback.downloadError")
      setFeedback({ type: "error", message })
    }
  }

  function openScheduleHistory(scheduleId: number) {
    setSelectedScheduleId(scheduleId)
    setHistoryRequestVersion((current) => current + 1)
    if (typeof window !== "undefined") {
      window.requestAnimationFrame(() => {
        historySectionRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
      })
    }
  }

  useEffect(() => {
    void refreshSchedules()
    void refreshNotificationRecipients()
  }, [])

  useEffect(() => {
    if (!queries.length || scheduleValues.query_id) {
      return
    }

    const defaultQuery = queries[0]
    setScheduleFieldValue("query_id", defaultQuery.id)

    if (defaultQuery.client_ids?.length) {
      setScheduleFieldValue("client_ids", getValidClientIds(defaultQuery.client_ids))
    }
  }, [clients, queries, scheduleValues.query_id, setScheduleFieldValue])

  useEffect(() => {
    if (!scheduleValues.client_ids.length) {
      return
    }

    const sanitizedClientIds = getValidClientIds(scheduleValues.client_ids)
    if (sanitizedClientIds.length !== scheduleValues.client_ids.length) {
      setScheduleFieldValue("client_ids", sanitizedClientIds)
    }
  }, [clients, scheduleValues.client_ids, setScheduleFieldValue])

  useEffect(() => {
    if (!selectedScheduleId) {
      setHistoryItems([])
      setHistoryError(null)
      setHistoryLoading(false)
      return
    }

    let isActive = true

    const fetchHistory = async () => {
      try {
        setHistoryLoading(true)
        setHistoryError(null)
        setHistoryItems([])
        const nextHistoryItems = await api.getScheduleHistory(selectedScheduleId)

        if (isActive) {
          setHistoryItems(nextHistoryItems)
        }
      } catch (error) {
        if (isActive) {
          const message = error instanceof Error ? error.message : t("schedules:feedback.loadHistoryError")
          setHistoryError(message)
        }
      } finally {
        if (isActive) {
          setHistoryLoading(false)
        }
      }
    }

    void fetchHistory()

    return () => {
      isActive = false
    }
  }, [selectedScheduleId, historyRequestVersion])

  const scheduleRows = schedules.map((schedule) => {
    const queryLabel =
      schedule.query_title ||
      queries.find((query) => query.id === schedule.query_id)?.title ||
      t("schedules:queryFallback", { id: schedule.query_id })
    const clientLabels = schedule.client_ids.map((clientId) => {
      const client = clients.find((item) => item.id === clientId)
      return client?.name || t("schedules:clientFallback", { id: clientId })
    })
    const lookbackValue = schedule.lookback_value ?? schedule.days_back ?? 1
    const lookbackUnit = schedule.lookback_unit ?? "days"
    const notifyOnResults = Boolean(schedule.notify_on_results)

    return {
      ...schedule,
      queryLabel,
      clientLabels,
      clientLabel: clientLabels.join(", "),
      lookbackLabel: formatDuration(lookbackValue, lookbackUnit, t),
      intervalLabel: formatDuration(schedule.interval_value, schedule.interval_unit, t),
      notifyOnResults,
    }
  })

  const totalRuns = historyItems.length
  const successfulRuns = historyItems.filter((historyItem) => isSuccessfulStatus(historyItem.status)).length
  const failedRuns = historyItems.filter((historyItem) => isErrorStatus(historyItem.status)).length
  const totalResults = historyItems.reduce((total, historyItem) => total + Math.max(getHistoryResultCount(historyItem) || 0, 0), 0)
  const readyToCreate = queries.length > 0 && availableClients.length > 0
  const formBusy = isScheduleSubmitting || queriesLoading || clientsLoading

  // Histórico filtrado (status + busca por ambiente) e paginado, para que a
  // tabela não cresça indefinidamente e seja navegável.
  const selectedQueryLabel =
    selectedSchedule?.query_title ||
    queries.find((query) => query.id === selectedSchedule?.query_id)?.title ||
    (selectedSchedule ? t("schedules:queryFallback", { id: selectedSchedule.query_id }) : "")

  const filteredHistory = useMemo(() => {
    const search = historySearch.trim().toLowerCase()
    return historyItems.filter((item) => {
      if (historyStatusFilter === "success" && !isSuccessfulStatus(item.status)) return false
      if (historyStatusFilter === "error" && !isErrorStatus(item.status)) return false
      if (historyStatusFilter === "running" && (isSuccessfulStatus(item.status) || isErrorStatus(item.status))) return false
      if (search) {
        const environment = (getClientName(item.client_id) || "").toLowerCase()
        if (!environment.includes(search) && !item.search_id.toLowerCase().includes(search)) return false
      }
      return true
    })
  }, [historyItems, historyStatusFilter, historySearch, clients])

  const totalHistoryPages = Math.max(1, Math.ceil(filteredHistory.length / HISTORY_PAGE_SIZE))
  const currentHistoryPage = Math.min(historyPage, totalHistoryPages)
  const paginatedHistory = filteredHistory.slice(
    (currentHistoryPage - 1) * HISTORY_PAGE_SIZE,
    currentHistoryPage * HISTORY_PAGE_SIZE,
  )
  const historyRangeStart = filteredHistory.length === 0 ? 0 : (currentHistoryPage - 1) * HISTORY_PAGE_SIZE + 1
  const historyRangeEnd = Math.min(currentHistoryPage * HISTORY_PAGE_SIZE, filteredHistory.length)

  // Reseta para a primeira página quando muda o agendamento ou os filtros.
  useEffect(() => {
    setHistoryPage(1)
  }, [selectedScheduleId, historyStatusFilter, historySearch])

  return (
    <div className="space-y-6">
      <PageHeader
        icon={<CalendarIcon size={24} />}
        eyebrow={t("schedules:pageEyebrow")}
        title={t("schedules:pageTitle")}
        description={t("schedules:pageDescription")}
        actions={
          <Button
            variant="outline"
            onClick={() => void handleRefresh()}
            leftIcon={<RefreshCcwIcon size={16} />}
            disabled={schedulesLoading || queriesLoading || clientsLoading}
          >
            {t("common:actions.refresh")}
          </Button>
        }
      />

      <div className="grid gap-4 sm:grid-cols-4">
        {[
          { label: t("schedules:stats.schedules"), value: schedules.length, tone: "primary" as const },
          { label: t("schedules:stats.readyQueries"), value: queries.length, tone: "default" as const },
          { label: t("schedules:stats.eligibleClients"), value: availableClients.length, tone: "success" as const },
          { label: t("schedules:stats.activeEmails"), value: notificationRecipientsCount, tone: "warning" as const },
        ].map((item) => (
          <Card key={item.label} padding="sm" className="shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div>
                <div className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{item.label}</div>
                <div className="mt-2 text-2xl font-bold text-text">{item.value}</div>
              </div>
              <Badge variant={item.tone} size="lg">
                {item.value}
              </Badge>
            </div>
          </Card>
        ))}
      </div>

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"} title={feedback.type === "success" ? t("schedules:feedback.operationCompleted") : t("schedules:feedback.operationFailed")}>
          {feedback.message}
        </Notice>
      )}

      {(queriesError || clientsError) && (
        <Notice variant="danger" title={t("schedules:feedback.dependenciesUnavailable")}>
          {queriesError || clientsError}
        </Notice>
      )}

      <div className="space-y-6">
        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle>{t("schedules:form.title")}</CardTitle>
            <CardDescription>{t("schedules:form.description")}</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {!readyToCreate && !formBusy && (
                <Notice variant="warning" title={t("schedules:form.pendingSetupTitle")}>
                  {t("schedules:form.pendingSetupDescription")}
                </Notice>
              )}

              {scheduleValues.notify_on_results && notificationRecipientsCount === 0 && (
                <Notice variant="warning" title={t("schedules:form.noRecipientsTitle")} icon={<BellIcon size={16} />}>
                  {t("schedules:form.noRecipientsDescription")}
                </Notice>
              )}

              <form className="grid gap-4 md:grid-cols-2" onSubmit={handleScheduleSubmit} noValidate>
                <div className="md:col-span-2">
                  <Select
                    label={t("schedules:form.fields.query")}
                    required
                    options={queries.map((query) => ({ value: query.id, label: query.title }))}
                    value={scheduleValues.query_id || undefined}
                    onValueChange={handleScheduleQueryChange}
                    disabled={formBusy || !queries.length}
                    error={scheduleErrors.query_id}
                    helperText={t("schedules:form.fields.queryHelperText")}
                  />
                </div>

                <div className="md:col-span-2">
                  <Select
                    label={t("schedules:form.fields.clients")}
                    required
                    multiple
                    options={availableClients.map((client) => ({ value: client.id, label: client.name }))}
                    value={scheduleValues.client_ids}
                    onValueChange={handleClientSelectionChange}
                    disabled={formBusy || !availableClients.length}
                    error={scheduleErrors.client_ids}
                  />
                </div>

                <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_140px]">
                  <Input
                    name="lookback_value"
                    type="number"
                    min={1}
                    step={1}
                    label={t("schedules:form.fields.lookbackWindow")}
                    required
                    value={scheduleValues.lookback_value}
                    onChange={handleScheduleChange}
                    error={scheduleErrors.lookback_value}
                    disabled={formBusy}
                  />
                  <Select
                    label={t("schedules:form.fields.unit")}
                    options={scheduleUnitOptions.map((option) => ({ value: option.value, label: option.label }))}
                    value={scheduleValues.lookback_unit}
                    onValueChange={(value) => handleUnitSelectionChange("lookback_unit", value)}
                    disabled={formBusy}
                  />
                </div>

                <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_140px]">
                  <Input
                    name="interval_value"
                    type="number"
                    min={1}
                    step={1}
                    label={t("schedules:form.fields.runEvery")}
                    required
                    value={scheduleValues.interval_value}
                    onChange={handleScheduleChange}
                    error={scheduleErrors.interval_value}
                    disabled={formBusy}
                  />
                  <Select
                    label={t("schedules:form.fields.unit")}
                    options={scheduleUnitOptions.map((option) => ({ value: option.value, label: option.label }))}
                    value={scheduleValues.interval_unit}
                    onValueChange={(value) => handleUnitSelectionChange("interval_unit", value)}
                    disabled={formBusy}
                  />
                </div>

                <label className="flex items-start gap-3 rounded-xl border border-border bg-surface-tertiary/40 p-4 text-sm text-text md:col-span-2">
                  <input
                    type="checkbox"
                    name="notify_on_results"
                    checked={scheduleValues.notify_on_results}
                    onChange={handleScheduleChange}
                    disabled={formBusy}
                    className="mt-0.5 h-4 w-4 rounded border-border text-primary-600 focus:ring-primary-500"
                  />
                  <span>
                    <span className="font-medium">{t("schedules:form.fields.notifyByEmail")}</span>
                    <span className="mt-1 block text-xs text-text-secondary">
                      {t("schedules:form.fields.configuredEmails")} <strong>{notificationRecipientsCount}</strong>
                    </span>
                  </span>
                </label>

                <div className="flex justify-end md:col-span-2">
                  <Button type="submit" loading={isScheduleSubmitting} disabled={!readyToCreate || formBusy}>
                    {t("schedules:form.submit")}
                  </Button>
                </div>
              </form>
            </div>
          </CardContent>
        </Card>

        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle>
              {t("schedules:activeList.title")}
              {scheduleRows.length > 0 && (
                <Badge variant="primary" size="sm" className="ml-2">
                  {scheduleRows.length}
                </Badge>
              )}
            </CardTitle>
            <CardDescription>{t("schedules:activeList.description")}</CardDescription>
          </CardHeader>
          <CardContent>
            {schedulesLoading ? (
              <div className="flex min-h-[280px] items-center justify-center">
                <LoadingSpinner text={t("schedules:activeList.loading")} />
              </div>
            ) : scheduleRows.length === 0 ? (
              <EmptyState
                title={t("schedules:activeList.emptyTitle")}
                description={t("schedules:activeList.emptyDescription")}
              />
            ) : (
              <>
                {/* Tablet / desktop: tabela com cabeçalho fixo e rolagem segura. */}
                <div className="hidden overflow-hidden rounded-xl border border-border md:block">
                  <div className="max-h-[30rem] overflow-auto">
                    <table className="w-full min-w-[920px] text-sm" role="table" aria-label={t("schedules:activeList.tableAriaLabel")}>
                      <thead className="sticky top-0 z-10 bg-surface-tertiary">
                        <tr className="border-b border-border">
                          <th className={thCls}>{t("schedules:activeList.columns.query")}</th>
                          <th className={thCls}>{t("schedules:activeList.columns.clients")}</th>
                          <th className={`${thCls} whitespace-nowrap`}>{t("schedules:activeList.columns.window")}</th>
                          <th className={`${thCls} whitespace-nowrap`}>{t("schedules:activeList.columns.recurrence")}</th>
                          <th className={`${thCls} whitespace-nowrap`}>{t("schedules:activeList.columns.nextRun")}</th>
                          <th className={`${thCls} whitespace-nowrap`}>{t("schedules:activeList.columns.notification")}</th>
                          <th className={`${thCls} text-right`}>{t("common:fields.actions")}</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-border bg-surface">
                        {scheduleRows.map((schedule) => (
                          <tr
                            key={schedule.id}
                            className={schedule.id === selectedScheduleId ? "bg-primary-50/50" : "transition-colors hover:bg-surface-tertiary/40"}
                          >
                            <td className={tdCls}>
                              <div className="max-w-[260px] space-y-1">
                                <div className="line-clamp-2 font-semibold text-text" title={schedule.queryLabel}>
                                  {schedule.queryLabel}
                                </div>
                                <div className="text-xs text-text-tertiary">{t("schedules:activeList.idLabel", { id: schedule.id })}</div>
                              </div>
                            </td>
                            <td className={tdCls}>
                              <div className="flex max-w-[220px] flex-wrap items-center gap-1.5" title={schedule.clientLabel || undefined}>
                                {schedule.clientLabels.length === 0 && <span className="text-text-tertiary">—</span>}
                                {schedule.clientLabels.slice(0, MAX_VISIBLE_CLIENTS).map((name) => (
                                  <Badge key={name} variant="outline" size="sm">
                                    {name}
                                  </Badge>
                                ))}
                                {schedule.clientLabels.length > MAX_VISIBLE_CLIENTS && (
                                  <Badge variant="default" size="sm">
                                    +{schedule.clientLabels.length - MAX_VISIBLE_CLIENTS}
                                  </Badge>
                                )}
                              </div>
                            </td>
                            <td className={`${tdCls} whitespace-nowrap`}>{schedule.lookbackLabel}</td>
                            <td className={`${tdCls} whitespace-nowrap`}>{schedule.intervalLabel}</td>
                            <td className={`${tdCls} whitespace-nowrap`}>{formatDateTime(schedule.next_run, t)}</td>
                            <td className={tdCls}>
                              <Badge variant={schedule.notifyOnResults ? "success" : "outline"} size="sm">
                                {schedule.notifyOnResults ? t("schedules:activeList.notificationOn") : t("schedules:activeList.notificationOff")}
                              </Badge>
                            </td>
                            <td className={tdCls}>
                              <div className="flex justify-end gap-2 whitespace-nowrap">
                                <Button size="sm" variant="ghost" leftIcon={<EyeIcon size={14} />} onClick={() => openScheduleHistory(schedule.id)}>
                                  {t("schedules:activeList.history")}
                                </Button>
                                <Button
                                  size="sm"
                                  variant="danger"
                                  leftIcon={<Trash2Icon size={14} />}
                                  onClick={() => setDeleteCandidate(schedule)}
                                  disabled={deletingScheduleId === schedule.id}
                                >
                                  {t("common:actions.delete")}
                                </Button>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>

                {/* Mobile: cada agendamento vira um cartão — nada de scroll horizontal. */}
                <div className="space-y-3 md:hidden">
                  {scheduleRows.map((schedule) => (
                    <div
                      key={schedule.id}
                      className={`rounded-xl border p-4 ${
                        schedule.id === selectedScheduleId ? "border-primary-200 bg-primary-50/50" : "border-border bg-surface"
                      }`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <div className="min-w-0">
                          <div className="font-semibold text-text" title={schedule.queryLabel}>{schedule.queryLabel}</div>
                          <div className="text-xs text-text-tertiary">{t("schedules:activeList.idLabel", { id: schedule.id })}</div>
                        </div>
                        <Badge variant={schedule.notifyOnResults ? "success" : "outline"} size="sm">
                          {schedule.notifyOnResults ? t("schedules:activeList.notifies") : t("schedules:activeList.noNotice")}
                        </Badge>
                      </div>
                      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
                        <div className="col-span-2">
                          <dt className="text-text-tertiary">{t("schedules:activeList.columns.clients")}</dt>
                          <dd className="text-text">{schedule.clientLabel || "—"}</dd>
                        </div>
                        <div>
                          <dt className="text-text-tertiary">{t("schedules:activeList.columns.window")}</dt>
                          <dd className="text-text">{schedule.lookbackLabel}</dd>
                        </div>
                        <div>
                          <dt className="text-text-tertiary">{t("schedules:activeList.columns.recurrence")}</dt>
                          <dd className="text-text">{schedule.intervalLabel}</dd>
                        </div>
                        <div className="col-span-2">
                          <dt className="text-text-tertiary">{t("schedules:activeList.columns.nextRun")}</dt>
                          <dd className="text-text">{formatDateTime(schedule.next_run, t)}</dd>
                        </div>
                      </dl>
                      <div className="mt-3 flex gap-2">
                        <Button size="sm" variant="ghost" leftIcon={<EyeIcon size={14} />} onClick={() => openScheduleHistory(schedule.id)}>
                          {t("schedules:activeList.history")}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          leftIcon={<Trash2Icon size={14} />}
                          onClick={() => setDeleteCandidate(schedule)}
                          disabled={deletingScheduleId === schedule.id}
                        >
                          {t("common:actions.delete")}
                        </Button>
                      </div>
                    </div>
                  ))}
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>

      <div ref={historySectionRef}>
        <Card className="shadow-sm">
          <CardHeader>
            <CardTitle>
              <BarChart3Icon size={18} />
              {t("schedules:history.title")}
            </CardTitle>
            <CardDescription>
              {selectedSchedule
                ? t("schedules:history.subtitle", { name: selectedSchedule.query_title || `#${selectedSchedule.id}` })
                : t("schedules:history.selectPrompt")}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {!selectedSchedule ? (
              <EmptyState
                title={t("schedules:history.noneSelectedTitle")}
                description={t("schedules:history.noneSelectedDescription")}
              />
            ) : (
              <div className="space-y-6">
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
                  {[
                    { label: t("schedules:history.stats.lastRun"), value: formatDateTime(selectedSchedule.last_run_at, t) },
                    { label: t("schedules:history.stats.totalRuns"), value: totalRuns },
                    { label: t("schedules:history.stats.successfulRuns"), value: successfulRuns },
                    { label: t("schedules:history.stats.failedRuns"), value: failedRuns },
                    { label: t("schedules:history.stats.accumulatedResults"), value: totalResults },
                  ].map((stat) => (
                    <div key={stat.label} className="rounded-xl border border-border bg-surface-tertiary/40 p-4">
                      <span className="block text-xs font-semibold uppercase tracking-wider text-text-tertiary">{stat.label}</span>
                      <strong className="mt-1 block text-lg font-bold text-text">{stat.value}</strong>
                    </div>
                  ))}
                </div>

                {historyError && (
                  <Notice variant="danger" title={t("schedules:history.loadErrorTitle")}>
                    {historyError}
                  </Notice>
                )}

                {historyLoading ? (
                  <LoadingSpinner text={t("schedules:history.loading")} />
                ) : historyItems.length === 0 ? (
                  <EmptyState
                    title={t("schedules:history.emptyTitle")}
                    description={t("schedules:history.emptyDescription")}
                  />
                ) : (
                  <div className="space-y-4">
                    {/* Barra de filtros — status + busca por ambiente. */}
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="inline-flex flex-wrap rounded-lg border border-border bg-surface p-0.5" role="group" aria-label={t("schedules:history.filterByStatusAriaLabel")}>
                        {historyStatusFilters.map((filter) => (
                          <button
                            key={filter.value}
                            type="button"
                            aria-pressed={historyStatusFilter === filter.value}
                            onClick={() => setHistoryStatusFilter(filter.value)}
                            className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                              historyStatusFilter === filter.value
                                ? "bg-primary-600 text-white"
                                : "text-text-secondary hover:bg-surface-tertiary hover:text-text"
                            }`}
                          >
                            {filter.label}
                          </button>
                        ))}
                      </div>
                      <div className="relative sm:w-72">
                        <SearchIcon size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-text-tertiary" aria-hidden="true" />
                        <input
                          type="search"
                          value={historySearch}
                          onChange={(event) => setHistorySearch(event.target.value)}
                          placeholder={t("schedules:history.searchPlaceholder")}
                          aria-label={t("schedules:history.searchAriaLabel")}
                          className="h-9 w-full rounded-md border border-border bg-surface pl-9 pr-3 text-sm text-text placeholder:text-text-tertiary focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500/20"
                        />
                      </div>
                    </div>

                    {filteredHistory.length === 0 ? (
                      <EmptyState
                        title={t("schedules:history.noMatchTitle")}
                        description={t("schedules:history.noMatchDescription")}
                      />
                    ) : (
                      <div className="overflow-hidden rounded-xl border border-border">
                        <div className="max-h-[34rem] overflow-auto">
                          <table className="w-full min-w-[760px] text-sm" role="table" aria-label={t("schedules:history.tableAriaLabel")}>
                            <thead className="sticky top-0 z-10 bg-surface-tertiary">
                              <tr className="border-b border-border">
                                <th className={`${thCls} whitespace-nowrap`}>{t("schedules:history.columns.when")}</th>
                                <th className={`${thCls} whitespace-nowrap`}>{t("common:fields.status")}</th>
                                <th className={thCls}>{t("schedules:history.columns.queryEnvironment")}</th>
                                <th className={`${thCls} whitespace-nowrap`}>{t("schedules:history.columns.period")}</th>
                                <th className={`${thCls} whitespace-nowrap`}>{t("schedules:history.columns.results")}</th>
                                <th className={`${thCls} text-right`}>
                                  <span className="sr-only">{t("common:fields.actions")}</span>
                                </th>
                              </tr>
                            </thead>
                            <tbody className="divide-y divide-border bg-surface">
                              {paginatedHistory.map((historyItem) => {
                                const environment = getClientName(historyItem.client_id)
                                return (
                                  <tr key={historyItem.id} className="transition-colors hover:bg-surface-tertiary/40">
                                    <td className={`${tdCls} whitespace-nowrap`}>{formatDateTime(historyItem.created_at, t)}</td>
                                    <td className={tdCls}>
                                      <span className={getStatusClassName(historyItem.status)}>
                                        {getStatusLabel(historyItem.status, t)}
                                      </span>
                                    </td>
                                    <td className={tdCls}>
                                      <div className="max-w-[280px] space-y-1">
                                        <div className="line-clamp-1 font-medium text-text" title={selectedQueryLabel}>
                                          {selectedQueryLabel}
                                        </div>
                                        <div className="flex items-center gap-1.5 text-xs text-text-secondary">
                                          <Building2Icon size={13} className="shrink-0 text-text-tertiary" aria-hidden="true" />
                                          <span className="truncate" title={environment || undefined}>
                                            {environment || t("schedules:history.environmentUnknown")}
                                          </span>
                                        </div>
                                      </div>
                                    </td>
                                    <td className={`${tdCls} whitespace-nowrap`}>
                                      <div className="space-y-1">
                                        <strong className="block font-medium text-text">{formatDateTime(historyItem.from_ts, t)}</strong>
                                        <span className="block text-xs text-text-tertiary">{t("schedules:history.until")} {formatDateTime(historyItem.to_ts, t)}</span>
                                      </div>
                                    </td>
                                    <td className={`${tdCls} whitespace-nowrap tabular-nums`}>{getHistoryResultCount(historyItem) ?? t("schedules:history.notAvailable")}</td>
                                    <td className={tdCls}>
                                      <div className="flex justify-end gap-2 whitespace-nowrap">
                                        <Button
                                          size="sm"
                                          variant="ghost"
                                          leftIcon={<EyeIcon size={14} />}
                                          onClick={() => setPreviewItem(historyItem)}
                                        >
                                          {t("common:actions.view")}
                                        </Button>
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          leftIcon={<DownloadIcon size={14} />}
                                          onClick={() => void handleDownloadResult(historyItem)}
                                          disabled={(getHistoryResultCount(historyItem) || 0) <= 0}
                                        >
                                          {t("schedules:history.csv")}
                                        </Button>
                                      </div>
                                    </td>
                                  </tr>
                                )
                              })}
                            </tbody>
                          </table>
                        </div>

                        {/* Paginação — mantém a página enxuta mesmo com muito histórico. */}
                        {totalHistoryPages > 1 && (
                          <div className="flex flex-col gap-3 border-t border-border bg-surface-tertiary/30 px-4 py-3 text-sm text-text-secondary sm:flex-row sm:items-center sm:justify-between">
                            <span>
                              {t("schedules:history.pagination.showing")} <strong className="text-text">{historyRangeStart}–{historyRangeEnd}</strong> {t("schedules:history.pagination.of")}{" "}
                              <strong className="text-text">{filteredHistory.length}</strong> {t("schedules:history.pagination.runs")}
                            </span>
                            <div className="flex items-center gap-3">
                              <Button
                                size="sm"
                                variant="outline"
                                leftIcon={<ChevronLeftIcon size={14} />}
                                disabled={currentHistoryPage <= 1}
                                onClick={() => setHistoryPage((page) => Math.max(1, page - 1))}
                              >
                                {t("schedules:history.pagination.previous")}
                              </Button>
                              <span className="text-xs">
                                {t("schedules:history.pagination.pageOf", { current: currentHistoryPage, total: totalHistoryPages })}
                              </span>
                              <Button
                                size="sm"
                                variant="outline"
                                rightIcon={<ChevronRightIcon size={14} />}
                                disabled={currentHistoryPage >= totalHistoryPages}
                                onClick={() => setHistoryPage((page) => Math.min(totalHistoryPages, page + 1))}
                              >
                                {t("schedules:history.pagination.next")}
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <ConfirmDialog
        open={!!deleteCandidate}
        title={t("schedules:deleteDialog.title")}
        description={
          <p>
            {t("schedules:deleteDialog.confirmPrefix")} <strong>{deleteCandidate?.query_title || t("schedules:queryFallback", { id: deleteCandidate?.query_id })}</strong>
            {t("schedules:deleteDialog.confirmSuffix")}
          </p>
        }
        confirmLabel={t("schedules:deleteDialog.confirmLabel")}
        loading={deletingScheduleId === deleteCandidate?.id}
        onConfirm={confirmDeleteSchedule}
        onClose={() => setDeleteCandidate(null)}
      />

      <Modal open={!!previewItem} onClose={() => setPreviewItem(null)} title={t("schedules:previewModal.title")} size="xl">
        {previewItem && (
          <div className="space-y-5">
            <div className="grid gap-4 sm:grid-cols-3">
              <div>
                <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("schedules:previewModal.searchId")}</span>
                <strong className="mt-2 block font-mono text-sm text-text">{previewItem.search_id}</strong>
              </div>
              <div>
                <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("common:fields.status")}</span>
                <strong className="mt-2 block text-sm text-text">{previewItem.status}</strong>
              </div>
              <div>
                <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">{t("schedules:previewModal.results")}</span>
                <strong className="mt-2 block text-sm text-text">{getHistoryResultCount(previewItem) ?? t("schedules:history.notAvailable")}</strong>
              </div>
            </div>

            {previewItem.error_message && (
              <Notice variant="danger" title={t("schedules:previewModal.errorTitle")}>
                {previewItem.error_message}
              </Notice>
            )}

            <pre className="max-h-[480px] overflow-auto rounded-xl border border-border bg-surface-tertiary/50 p-4 text-xs leading-relaxed text-text-secondary">{formatResultPayload(previewItem, t)}</pre>

            <div className="flex justify-end">
              <Button
                size="sm"
                variant="outline"
                leftIcon={<DownloadIcon size={14} />}
                onClick={() => void handleDownloadResult(previewItem)}
                disabled={(getHistoryResultCount(previewItem) || 0) <= 0}
              >
                {t("schedules:previewModal.downloadCsv")}
              </Button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  )
}

export default SchedulesPage
