import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useTranslation } from "react-i18next"
import {
  ArrowLeftIcon,
  SendIcon,
  PlayIcon,
  EyeIcon,
  RefreshCcwIcon,
  HeartPulseIcon,
  SettingsIcon,
  InboxIcon,
  RadioIcon,
  KeyRoundIcon,
  RotateCcwIcon,
} from "lucide-react"
import * as api from "@/services/api"
import { PageHeader } from "@/components/ui/PageHeader/PageHeader"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"
import { Sparkline } from "@/components/observability/Sparkline"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { DestinationForm } from "@/components/destinations/DestinationForm"
import { CredentialPanel } from "@/components/destinations/CredentialPanel"
import { LineageLookup } from "@/components/destinations/LineageLookup"
import { formatDateTime } from "@/lib/intl"
import type {
  Destination,
  DestinationCreateRequest,
  DestinationHealth,
  DestinationHealthStatus,
  DestinationUpdateRequest,
  DestinationDlqResponse,
  DestinationMetrics,
} from "@/types"

type Feedback = { type: "success" | "error"; message: string }

const STATUS_VARIANT: Record<DestinationHealthStatus, "success" | "warning" | "danger" | "default" | "outline"> = {
  healthy: "success",
  degraded: "warning",
  unhealthy: "danger",
  disabled: "default",
  unknown: "outline",
}

const DestinationDetailPage: React.FC = () => {
  const { t } = useTranslation("routing")
  const { id = "" } = useParams<{ id: string }>()
  const navigate = useNavigate()

  const STATUS_LABEL: Record<DestinationHealthStatus, string> = useMemo(
    () => ({
      healthy: t("detailPage.statusLabels.healthy"),
      degraded: t("detailPage.statusLabels.degraded"),
      unhealthy: t("detailPage.statusLabels.unhealthy"),
      disabled: t("detailPage.statusLabels.disabled"),
      unknown: t("detailPage.statusLabels.unknown"),
    }),
    [t],
  )

  /** Formata bytes em unidade legível (B, KB, MB, GB). */
  const fmtBytes = useCallback(
    (bytes: number): string => {
      if (bytes < 1024) return t("detailPage.bytesUnit.perMinB", { value: bytes.toFixed(0) })
      if (bytes < 1024 * 1024) return t("detailPage.bytesUnit.perMinKB", { value: (bytes / 1024).toFixed(1) })
      if (bytes < 1024 * 1024 * 1024) return t("detailPage.bytesUnit.perMinMB", { value: (bytes / 1024 / 1024).toFixed(1) })
      return t("detailPage.bytesUnit.perMinGB", { value: (bytes / 1024 / 1024 / 1024).toFixed(2) })
    },
    [t],
  )

  const [destination, setDestination] = useState<Destination | null>(null)
  const [health, setHealth] = useState<DestinationHealth | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const [tab, setTab] = useState("config")
  const [shadowPreview, setShadowPreview] = useState<string | null>(null)
  const [shadowing, setShadowing] = useState(false)
  const [testing, setTesting] = useState(false)
  const [metrics, setMetrics] = useState<DestinationMetrics | null>(null)
  const [dlq, setDlq] = useState<DestinationDlqResponse | null>(null)
  const [expandedDlq, setExpandedDlq] = useState<string | null>(null)
  const [tap, setTap] = useState<Record<string, unknown>[] | null>(null)

  // DLQ reprocess state
  const [reprocessAll, setReprocessAll] = useState(false)
  const [reprocessEntryId, setReprocessEntryId] = useState<string | null>(null)
  const [reprocessing, setReprocessing] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const d = await api.getDestination(id)
      setDestination(d)
    } catch (err) {
      setError(err instanceof Error ? err.message : t("detailPage.notFound"))
    } finally {
      setLoading(false)
    }
  }, [id, t])

  const loadHealth = useCallback(async () => {
    try {
      setHealth(await api.getDestinationHealth(id))
    } catch {
      setHealth(null)
    }
    try {
      setMetrics(await api.getDestinationMetrics(id, { range_minutes: 60 }))
    } catch {
      setMetrics(null)
    }
  }, [id])

  const loadDlq = useCallback(async () => {
    try {
      setDlq(await api.getDestinationDlq(id, { limit: 50 }))
    } catch {
      setDlq(null)
    }
  }, [id])

  const loadTap = useCallback(async () => {
    try {
      setTap((await api.getDestinationTap(id, { limit: 50 })).entries)
    } catch {
      setTap(null)
    }
  }, [id])

  useEffect(() => {
    void load()
    void loadHealth()
    void loadDlq()
    void loadTap()
  }, [load, loadHealth, loadDlq, loadTap])

  useEffect(() => {
    if (!feedback) return
    const t = setTimeout(() => setFeedback(null), 6000)
    return () => clearTimeout(t)
  }, [feedback])

  const handleUpdate = async (payload: DestinationCreateRequest | DestinationUpdateRequest) => {
    setSaving(true)
    try {
      const updated = await api.updateDestination(id, payload as DestinationUpdateRequest)
      setDestination(updated)
      setFeedback({ type: "success", message: t("detailPage.updateSuccess") })
      void loadHealth()
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTesting(true)
    try {
      const r = await api.testDestination(id)
      const detail = r.detail ? ` — ${r.detail}` : ""
      const latency = r.latency_ms != null ? ` (${r.latency_ms.toFixed(0)} ms)` : ""
      setFeedback({
        type: r.ok ? "success" : "error",
        message: r.ok
          ? t("detailPage.testSuccessDetail", { detail, latency })
          : t("detailPage.testFailureDetail", { detail, latency }),
      })
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("detailPage.testError") })
    } finally {
      setTesting(false)
    }
  }

  const handleShadow = async () => {
    setShadowing(true)
    setShadowPreview(null)
    try {
      const r = await api.shadowDestination(id)
      if (r.ok) {
        setShadowPreview(r.formatted_preview ?? t("detailPage.shadowEmpty"))
      } else {
        setFeedback({ type: "error", message: t("detailPage.shadowFailure", { detail: r.detail }) })
      }
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("detailPage.shadowError") })
    } finally {
      setShadowing(false)
    }
  }

  // ── DLQ reprocess ──────────────────────────────────────────────────────────

  const handleReprocess = async () => {
    const eventIds = reprocessEntryId ? [reprocessEntryId] : undefined
    setReprocessing(true)
    try {
      const r = await api.reprocessDestinationDlq(id, eventIds)
      setReprocessAll(false)
      setReprocessEntryId(null)
      setFeedback({
        type: "success",
        message: t("detailPage.reprocessQueued", { count: r.queued, taskId: r.task_id || "—" }),
      })
      void loadDlq()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("detailPage.reprocessError") })
    } finally {
      setReprocessing(false)
    }
  }

  if (loading) {
    return <Card padding="lg" className="text-center text-sm text-text-secondary">{t("detailPage.loading")}</Card>
  }
  if (error || !destination) {
    return (
      <div className="space-y-4">
        <Button variant="outline" size="sm" onClick={() => navigate("/destinations")} leftIcon={<ArrowLeftIcon size={14} />}>
          {t("common:actions.back")}
        </Button>
        <Notice variant="danger" title={t("detailPage.unavailableTitle")}>
          {error ?? t("detailPage.notFound")}
        </Notice>
      </div>
    )
  }

  const dlqReprocessAllDescription =
    dlq && dlq.total > 0
      ? t("detailPage.dlq.reprocessAllDescriptionWithCount", { count: dlq.total })
      : t("detailPage.dlq.reprocessAllDescriptionEmpty")

  const dlqReprocessEntryDescription = reprocessEntryId
    ? t("detailPage.dlq.reprocessEntryDescription", { eventId: reprocessEntryId })
    : ""

  return (
    <div className="space-y-6">
      <Button variant="ghost" size="sm" onClick={() => navigate("/destinations")} leftIcon={<ArrowLeftIcon size={14} />}>
        {t("detailPage.backToDestinations")}
      </Button>

      <PageHeader
        icon={<SendIcon size={24} />}
        eyebrow={destination.kind}
        title={destination.name}
        description={t("detailPage.idLabel", { id: destination.id })}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {health && (
              <Badge variant={STATUS_VARIANT[health.status] ?? "outline"}>
                {STATUS_LABEL[health.status] ?? health.status}
              </Badge>
            )}
            <Button variant="outline" onClick={() => void handleTest()} leftIcon={<PlayIcon size={16} />} loading={testing}>
              {t("detailPage.testAction")}
            </Button>
            <Button variant="outline" onClick={() => void handleShadow()} leftIcon={<EyeIcon size={16} />} loading={shadowing}>
              {t("detailPage.shadowAction")}
            </Button>
          </div>
        }
      />

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"} title={feedback.type === "success" ? t("detailPage.feedbackOkTitle") : t("detailPage.feedbackErrorTitle")}>
          {feedback.message}
        </Notice>
      )}

      {shadowPreview !== null && (
        <Card padding="md" className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-text">{t("detailPage.previewTitle")}</h3>
            <Button variant="ghost" size="xs" onClick={() => setShadowPreview(null)}>
              {t("detailPage.close")}
            </Button>
          </div>
          <pre className="max-h-64 overflow-auto rounded-md bg-surface-tertiary p-3 font-mono text-xs text-text">
            {shadowPreview}
          </pre>
        </Card>
      )}

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList ariaLabel={t("detailPage.tabsAriaLabel")}>
          <TabsTrigger value="config" icon={<SettingsIcon size={16} />}>
            {t("detailPage.configTab")}
          </TabsTrigger>
          <TabsTrigger value="health" icon={<HeartPulseIcon size={16} />}>
            {t("detailPage.healthTab")}
          </TabsTrigger>
          <TabsTrigger value="dlq" icon={<InboxIcon size={16} />} badge={dlq && dlq.total > 0 ? String(dlq.total) : undefined}>
            {t("detailPage.dlqTab")}
          </TabsTrigger>
          <TabsTrigger value="tap" icon={<RadioIcon size={16} />}>
            {t("detailPage.tapTab")}
          </TabsTrigger>
          {destination.has_secret && (
            <TabsTrigger value="credential" icon={<KeyRoundIcon size={16} />}>
              {t("detailPage.credentialTab")}
            </TabsTrigger>
          )}
        </TabsList>

        {/* ── Config ───────────────────────────────────────────────────────── */}
        <TabsPanel value="config">
          <Card padding="md">
            <DestinationForm
              mode="edit"
              destination={destination}
              loading={saving}
              onCancel={() => navigate("/destinations")}
              onSubmit={handleUpdate}
            />
          </Card>
        </TabsPanel>

        {/* ── Saúde ─────────────────────────────────────────────────────────── */}
        <TabsPanel value="health">
          <Card padding="md" className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-text">{t("detailPage.health.title")}</h3>
              <Button variant="outline" size="sm" onClick={() => void loadHealth()} leftIcon={<RefreshCcwIcon size={14} />}>
                {t("common:actions.refresh")}
              </Button>
            </div>

            {!health ? (
              <p className="text-sm text-text-tertiary">{t("detailPage.health.noData")}</p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                <Stat label={t("detailPage.health.status")} value={STATUS_LABEL[health.status] ?? health.status} />
                <Stat label={t("detailPage.health.circuitBreaker")} value={health.breaker_state ?? "—"} />
                <Stat label={t("detailPage.health.enabled")} value={health.enabled ? t("detailPage.health.yes") : t("detailPage.health.no")} />
                <Stat label={t("detailPage.health.dlq24h")} value={String(health.dlq_24h)} />
                <Stat label={t("detailPage.health.dlqTotal")} value={String(health.dlq_total)} />
                <Stat
                  label={t("detailPage.health.lastDlq")}
                  value={health.last_dlq_at ? formatDateTime(health.last_dlq_at) : "—"}
                />
                {/* eps e bytes_per_min adicionados (task C7 #1) */}
                {health.eps != null && (
                  <Stat label={t("detailPage.health.eps")} value={health.eps.toFixed(2)} />
                )}
                {health.bytes_per_min != null && (
                  <Stat label={t("detailPage.health.volumePerMin")} value={fmtBytes(health.bytes_per_min)} />
                )}
              </div>
            )}

            {/* Séries temporais */}
            <div className="space-y-3 border-t border-border pt-3">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-text-tertiary">{t("detailPage.health.seriesTitle")}</h4>
              {metrics && (metrics.series.sent?.length || metrics.series.rejected?.length) ? (
                <div className="flex flex-wrap gap-6">
                  {/* enviados → primary (positivo) */}
                  <Sparkline
                    points={metrics.series.sent ?? []}
                    label={t("detailPage.health.eventsPerMin")}
                    variant="primary"
                  />
                  {/* rejeitados → danger (sinal negativo) */}
                  <Sparkline
                    points={metrics.series.rejected ?? []}
                    label={t("detailPage.health.rejectedPerMin")}
                    variant="danger"
                  />
                  {/* descartados → danger se existir */}
                  {metrics.series.discarded?.length > 0 && (
                    <Sparkline
                      points={metrics.series.discarded}
                      label={t("detailPage.health.discardedPerMin")}
                      variant="danger"
                    />
                  )}
                  {/* latência → warning */}
                  <Sparkline
                    points={metrics.series.latency_avg ?? []}
                    label={t("detailPage.health.avgLatency")}
                    variant="warning"
                  />
                </div>
              ) : (
                <p className="text-xs text-text-tertiary">{t("detailPage.health.noRecentTraffic")}</p>
              )}

              {metrics && (metrics.gauges?.queue_depth != null || metrics.gauges?.backpressure_state != null) && (
                <div className="flex flex-wrap gap-3">
                  {metrics.gauges.queue_depth != null && <Badge variant="outline">{t("detailPage.health.queueDepth", { value: metrics.gauges.queue_depth })}</Badge>}
                  {metrics.gauges.backpressure_state != null && <Badge variant="warning">{t("detailPage.health.backpressure", { value: metrics.gauges.backpressure_state })}</Badge>}
                </div>
              )}
            </div>

            {/* Lineage por evento — seção dentro de Saúde */}
            <div className="border-t border-border pt-4">
              <LineageLookup destinationId={id} />
            </div>
          </Card>
        </TabsPanel>

        {/* ── DLQ ───────────────────────────────────────────────────────────── */}
        <TabsPanel value="dlq">
          <Card padding="md" className="space-y-4">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-text">{t("detailPage.dlq.title")}</h3>
              <div className="flex gap-2">
                {dlq && dlq.total > 0 && (
                  <Button
                    variant="primary"
                    size="sm"
                    leftIcon={<RotateCcwIcon size={14} />}
                    onClick={() => setReprocessAll(true)}
                    data-testid="btn-reprocess-all"
                  >
                    {t("detailPage.dlq.reprocessAll", { count: dlq.total })}
                  </Button>
                )}
                <Button variant="outline" size="sm" onClick={() => void loadDlq()} leftIcon={<RefreshCcwIcon size={14} />}>
                  {t("common:actions.refresh")}
                </Button>
              </div>
            </div>

            {!dlq || dlq.total === 0 ? (
              <p className="text-sm text-text-tertiary">{t("detailPage.dlq.empty")}</p>
            ) : (
              <>
                <div className="flex flex-wrap gap-2">
                  <Badge variant="outline">{t("detailPage.dlq.total", { count: dlq.total })}</Badge>
                  {Object.entries(dlq.by_error_kind).map(([k, n]) => (
                    <Badge key={k} variant="danger">{k}: {n}</Badge>
                  ))}
                </div>
                <div className="divide-y divide-border">
                  {dlq.entries.map((e) => (
                    <div key={e.id} className="py-2">
                      <button
                        type="button"
                        className="flex w-full items-center justify-between gap-2 text-left"
                        onClick={() => setExpandedDlq(expandedDlq === e.id ? null : e.id)}
                        aria-expanded={expandedDlq === e.id}
                        aria-controls={`dlq-payload-${e.id}`}
                      >
                        <span className="flex flex-wrap items-center gap-2">
                          <Badge variant="danger" size="sm">{e.error_kind}</Badge>
                          <span className="font-mono text-xs text-text-secondary">{e.event_id}</span>
                          {e.error_detail && <span className="text-xs text-text-tertiary">— {e.error_detail}</span>}
                        </span>
                        <div className="flex items-center gap-2">
                          <span className="text-xs text-text-tertiary">{formatDateTime(e.created_at)}</span>
                          <Button
                            variant="ghost"
                            size="xs"
                            leftIcon={<RotateCcwIcon size={12} />}
                            onClick={(ev) => {
                              ev.stopPropagation()
                              setReprocessEntryId(e.event_id)
                            }}
                            aria-label={t("detailPage.dlq.reprocessEntryAria", { eventId: e.event_id })}
                            data-testid={`btn-reprocess-entry-${e.event_id}`}
                          >
                            {t("detailPage.dlq.reprocessEntry")}
                          </Button>
                        </div>
                      </button>
                      {expandedDlq === e.id && (
                        <pre
                          id={`dlq-payload-${e.id}`}
                          className="mt-2 max-h-64 overflow-auto rounded-md bg-surface-tertiary p-3 font-mono text-xs text-text"
                        >
                          {e.payload ? JSON.stringify(e.payload, null, 2) : t("detailPage.dlq.noPayload")}
                        </pre>
                      )}
                    </div>
                  ))}
                </div>
              </>
            )}
          </Card>
        </TabsPanel>

        {/* ── Tap ao vivo ───────────────────────────────────────────────────── */}
        <TabsPanel value="tap">
          <Card padding="md" className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-text">{t("detailPage.tap.title")}</h3>
              <Button variant="outline" size="sm" onClick={() => void loadTap()} leftIcon={<RefreshCcwIcon size={14} />}>
                {t("common:actions.refresh")}
              </Button>
            </div>
            {!tap || tap.length === 0 ? (
              <p className="text-sm text-text-tertiary">{t("detailPage.tap.empty")}</p>
            ) : (
              <div className="space-y-2">
                {tap.map((e, i) => {
                  const meta = (e._centralops as Record<string, unknown>) || {}
                  return (
                    <details key={i} className="rounded-md border border-border bg-surface-secondary">
                      <summary className="cursor-pointer px-3 py-2 text-xs">
                        <span className="font-mono text-text-secondary">{String(meta.event_id ?? "—")}</span>
                        {meta.vendor != null && <span className="ml-2 text-text-tertiary">{String(meta.vendor)}</span>}
                        {meta.severity_id != null && <span className="ml-2 text-text-tertiary">sev {String(meta.severity_id)}</span>}
                      </summary>
                      <pre className="max-h-64 overflow-auto px-3 pb-3 font-mono text-xs text-text">
                        {JSON.stringify(e, null, 2)}
                      </pre>
                    </details>
                  )
                })}
              </div>
            )}
          </Card>
        </TabsPanel>

        {/* ── Credencial ────────────────────────────────────────────────────── */}
        {destination.has_secret && (
          <TabsPanel value="credential">
            <CredentialPanel
              destinationId={id}
              hasSecret={destination.has_secret}
              onRevoked={() => void load()}
            />
          </TabsPanel>
        )}
      </Tabs>

      {/* ConfirmDialog: reprocessar tudo */}
      <ConfirmDialog
        open={reprocessAll}
        title={t("detailPage.dlq.reprocessAllTitle")}
        description={dlqReprocessAllDescription}
        confirmLabel={t("detailPage.dlq.reprocessEntry")}
        confirmVariant="primary"
        loading={reprocessing}
        onConfirm={handleReprocess}
        onClose={() => setReprocessAll(false)}
        data-testid="dlq-reprocess-all-dialog"
      />

      {/* ConfirmDialog: reprocessar entrada única */}
      <ConfirmDialog
        open={reprocessEntryId !== null}
        title={t("detailPage.dlq.reprocessEntryTitle")}
        description={dlqReprocessEntryDescription}
        confirmLabel={t("detailPage.dlq.reprocessEntry")}
        confirmVariant="primary"
        loading={reprocessing}
        onConfirm={handleReprocess}
        onClose={() => setReprocessEntryId(null)}
        data-testid="dlq-reprocess-entry-dialog"
      />
    </div>
  )
}

const Stat: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="rounded-md border border-border bg-surface-secondary p-3">
    <div className="text-xs uppercase tracking-wide text-text-tertiary">{label}</div>
    <div className="mt-1 truncate text-sm font-medium text-text" title={value}>
      {value}
    </div>
  </div>
)

export default DestinationDetailPage
