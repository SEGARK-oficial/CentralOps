"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  ClipboardIcon,
  CopyIcon,
  EyeIcon,
  FilterIcon,
  RefreshCwIcon,
  TerminalIcon,
  Trash2Icon,
} from "lucide-react"
import * as api from "@/services/api"
import type { CollectorAuditEvent } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"

const FACILITY_LOCAL0 = 16
const SEVERITY_INFO = 6
const DEFAULT_PRI = FACILITY_LOCAL0 * 8 + SEVERITY_INFO // 134

/** Fallback usado apenas quando a entrada do ring não tem envelope
 *  (entradas legadas). Se aparecer, mostramos um aviso na UI. */
const HOSTNAME_FALLBACK = "centralops-audit-export"

/** Meses abreviados en-US para RFC 3164 (space-pad dia). */
const RFC3164_MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

/**
 * Formata a linha syslog wire conforme o formato usado no dispatch.
 *
 * - rfc3164 : `<PRI>Mmm dd HH:MM:SS hostname centralops[1]: {MSG}`
 *             PID fixo 1 (espelha `format_rfc3164` em `rfc3164_sender.py`).
 * - rfc5424 ou null (legado): comportamento original — RFC 5424 reconstituído.
 */
function buildWireLine(ev: CollectorAuditEvent): string {
  const meta = ev.meta ?? {}
  const envelope = ev.envelope ?? {}
  const pri = envelope.pri ?? DEFAULT_PRI
  const hostname = envelope.hostname ?? HOSTNAME_FALLBACK
  const msg = JSON.stringify(ev.event)

  if (ev.syslog_format === "rfc3164") {
    const ts = meta.collected_at ? new Date(meta.collected_at as string) : new Date()
    const month = RFC3164_MONTHS[ts.getUTCMonth()]
    const day = String(ts.getUTCDate()).padStart(2, " ")
    const hh = String(ts.getUTCHours()).padStart(2, "0")
    const mm = String(ts.getUTCMinutes()).padStart(2, "0")
    const ss = String(ts.getUTCSeconds()).padStart(2, "0")
    return `<${pri}>${month} ${day} ${hh}:${mm}:${ss} ${hostname} centralops[1]: ${msg}`
  }

  // RFC 5424 (default / legado)
  const ts = (meta.collected_at as string) ?? new Date().toISOString()
  const msgId = meta.integration_id ?? "-"
  const sd =
    `[centralops@32473 ` +
    `integration_id="${meta.integration_id ?? ""}" ` +
    `customer_id="${meta.customer_id ?? ""}" ` +
    `platform="${meta.platform ?? ""}" ` +
    `stream="${meta.stream ?? ""}"]`
  return `<${pri}>1 ${ts} ${hostname} centralops-collector - ${msgId} ${sd} ${msg}`
}

/** True quando a entry veio do ring antes do fix de fidelidade. */
function isLegacyEnvelope(ev: CollectorAuditEvent): boolean {
  return !ev.envelope || !ev.envelope.hostname
}

/** True quando o evento não tem envelope OCSF separado (pré-Fase 1). */
function isLegacyOcsfEnvelope(ev: CollectorAuditEvent): boolean {
  const event = ev.event ?? {}
  return !("normalized" in event) && !("raw" in event)
}

// Rótulos de formato syslog — nomes de protocolo, não traduzidos.
function formatLabel(ev: CollectorAuditEvent): string {
  if (ev.syslog_format === "rfc3164") return "RFC 3164"
  if (ev.syslog_format === "rfc5424") return "RFC 5424"
  return "RFC 5424"
}

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text)
  return new Promise((resolve) => {
    const el = document.createElement("textarea")
    el.value = text
    el.style.position = "fixed"
    el.style.opacity = "0"
    document.body.appendChild(el)
    el.select()
    document.execCommand("copy")
    document.body.removeChild(el)
    resolve()
  })
}

export const CollectorAuditPanel: React.FC = () => {
  const { t } = useTranslation("config")
  const [events, setEvents] = useState<CollectorAuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<
    { type: "success" | "error"; message: string } | null
  >(null)
  const [platformFilter, setPlatformFilter] = useState<string>("")
  const [streamFilter, setStreamFilter] = useState<string>("")
  const [limit, setLimit] = useState<number>(100)
  const [inspected, setInspected] = useState<CollectorAuditEvent | null>(null)
  const [inspectTab, setInspectTab] = useState<string>("wire")
  const [clearing, setClearing] = useState(false)
  const [confirmClear, setConfirmClear] = useState(false)
  // Catálogo completo de plataformas/streams suportados — vem do auto-discovery
  // do CollectorRegistry, não dos eventos do buffer. Garante que filtros mostrem
  // TODOS os streams configurados (não só os que casualmente estão no buffer).
  const [platformsCatalog, setPlatformsCatalog] = useState<Record<string, string[]>>({})

  const loadEvents = useCallback(async () => {
    try {
      setLoading(true)
      setError(null)
      const data = await api.getCollectorAuditRecent({
        limit,
        platform: platformFilter || undefined,
        stream: streamFilter || undefined,
      })
      setEvents(data.events)
    } catch (err) {
      setError(err instanceof Error ? err.message : t("audit.loadError"))
    } finally {
      setLoading(false)
    }
  }, [limit, platformFilter, streamFilter, t])

  useEffect(() => {
    void loadEvents()
  }, [loadEvents])

  // Carrega catálogo de plataformas/streams uma vez na montagem.
  // Falha não-fatal: se cair, dropdowns ficam vazios mas os eventos
  // ainda carregam normalmente.
  useEffect(() => {
    let cancelled = false
    api
      .listPlatformsStreams()
      .then((resp) => {
        if (!cancelled) setPlatformsCatalog(resp.platforms ?? {})
      })
      .catch(() => {
        // silencioso — não bloqueia o painel
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleClear = async () => {
    try {
      setClearing(true)
      await api.clearCollectorAudit()
      setFeedback({ type: "success", message: t("audit.clearBufferSuccess") })
      setConfirmClear(false)
      await loadEvents()
    } catch (err) {
      setFeedback({
        type: "error",
        message: err instanceof Error ? err.message : t("audit.clearBufferError"),
      })
    } finally {
      setClearing(false)
    }
  }

  const handleCopyWire = async (ev: CollectorAuditEvent) => {
    await copyToClipboard(buildWireLine(ev))
    const formatName = ev.syslog_format === "rfc3164" ? "RFC 3164" : "RFC 5424"
    setFeedback({
      type: "success",
      message: t("audit.copyWireSuccess", { format: formatName, tool: "wazuh-logtest" }),
    })
  }

  const handleCopyJson = async (ev: CollectorAuditEvent) => {
    await copyToClipboard(JSON.stringify(ev.event, null, 2))
    setFeedback({ type: "success", message: t("audit.copyJsonSuccess") })
  }

  const handleOpenInspect = (ev: CollectorAuditEvent) => {
    setInspected(ev)
    setInspectTab("wire")
  }

  // Plataformas e streams para os dropdowns. Fonte primária: catálogo do
  // registry (todas as plataformas suportadas, conhecidas no momento do
  // boot). Fonte secundária (defensiva): vendors/streams observados nos
  // eventos atualmente no buffer — cobre legados e plataformas registradas
  // após o último auto-discovery. A união evita que aplicar um filtro
  // colapse o dropdown (bug original: derivar do `events` filtrado fazia
  // o select perder valores enquanto o usuário ainda estava filtrando).
  //
  // Streams são contextuais à plataforma selecionada: se uma plataforma
  // está selecionada, só os streams dela aparecem; sem seleção, mostra a
  // união completa.
  const platforms = useMemo(() => {
    const set = new Set<string>(Object.keys(platformsCatalog))
    for (const e of events) {
      const v = e.meta.vendor ?? e.meta.platform
      if (v) set.add(v)
    }
    return Array.from(set).sort()
  }, [platformsCatalog, events])

  const streams = useMemo(() => {
    if (platformFilter) {
      const set = new Set<string>(platformsCatalog[platformFilter] ?? [])
      for (const e of events) {
        const v = e.meta.vendor ?? e.meta.platform
        const s = e.meta.event_type ?? e.meta.stream
        if (v === platformFilter && s) set.add(s)
      }
      return Array.from(set).sort()
    }
    const all = new Set<string>()
    for (const list of Object.values(platformsCatalog)) {
      for (const s of list) all.add(s)
    }
    for (const e of events) {
      const s = e.meta.event_type ?? e.meta.stream
      if (s) all.add(s)
    }
    return Array.from(all).sort()
  }, [platformsCatalog, platformFilter, events])

  // Quando o filtro de plataforma muda, se o stream selecionado não pertence
  // mais à nova plataforma, limpa o stream para evitar estado inválido.
  // Considera streams do catálogo + observados nos eventos (igual ao useMemo
  // de `streams`) — sem isso, eventos legados perderiam o filtro toda vez
  // que o catálogo carrega.
  useEffect(() => {
    if (!platformFilter || !streamFilter) return
    const validStreams = new Set<string>(platformsCatalog[platformFilter] ?? [])
    for (const e of events) {
      const v = e.meta.vendor ?? e.meta.platform
      const s = e.meta.event_type ?? e.meta.stream
      if (v === platformFilter && s) validStreams.add(s)
    }
    if (!validStreams.has(streamFilter)) {
      setStreamFilter("")
    }
  }, [platformFilter, streamFilter, platformsCatalog, events])

  return (
    <div className="space-y-4">
      <Notice variant="info" title={t("audit.intro.title")}>
        <Trans
          i18nKey="audit.intro.body"
          t={t}
          values={{ tool: "wazuh-logtest" }}
          components={{ strong: <strong /> }}
        />
      </Notice>

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>
          {feedback.message}
        </Notice>
      )}
      {error && <Notice variant="danger">{error}</Notice>}

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface p-3">
        <FilterIcon size={14} className="text-text-tertiary" />
        <select
          className="h-8 rounded border border-border bg-surface px-2 text-sm"
          value={platformFilter}
          onChange={(e) => setPlatformFilter(e.target.value)}
          aria-label={t("audit.toolbar.filterByPlatform")}
        >
          <option value="">{t("audit.toolbar.allPlatforms")}</option>
          {platforms.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <select
          className="h-8 rounded border border-border bg-surface px-2 text-sm"
          value={streamFilter}
          onChange={(e) => setStreamFilter(e.target.value)}
          aria-label={t("audit.toolbar.filterByStream")}
        >
          <option value="">{t("audit.toolbar.allStreams")}</option>
          {streams.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          className="h-8 rounded border border-border bg-surface px-2 text-sm"
          value={limit}
          onChange={(e) => setLimit(Number(e.target.value))}
          aria-label={t("audit.toolbar.quantity")}
        >
          {[25, 50, 100, 250, 500].map((n) => (
            <option key={n} value={n}>
              {t("audit.toolbar.eventsUnit", { count: n })}
            </option>
          ))}
        </select>

        <div className="ml-auto flex items-center gap-2">
          <Badge variant="outline" size="sm">
            {t("audit.toolbar.eventCount", { count: events.length })}
          </Badge>
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCwIcon size={14} />}
            onClick={() => void loadEvents()}
            loading={loading}
          >
            {t("common:actions.refresh")}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            leftIcon={<Trash2Icon size={14} />}
            onClick={() => setConfirmClear(true)}
            disabled={clearing || events.length === 0}
          >
            {t("audit.toolbar.clearBuffer")}
          </Button>
        </div>
      </div>

      {/* Lista */}
      {loading && events.length === 0 ? (
        <div className="flex justify-center py-10">
          <LoadingSpinner size="md" text={t("audit.loading")} />
        </div>
      ) : events.length === 0 ? (
        <EmptyState
          icon={<ClipboardIcon size={32} />}
          title={t("audit.emptyTitle")}
          description={t("audit.emptyDescription")}
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
              <tr>
                <th className="px-3 py-2 text-left">{t("audit.table.timestamp")}</th>
                <th className="px-3 py-2 text-left">{t("audit.table.vendorStream")}</th>
                <th className="px-3 py-2 text-left min-w-[120px]">{t("audit.table.customer")}</th>
                <th className="px-3 py-2 text-left min-w-[160px]">{t("audit.table.eventId")}</th>
                <th className="px-3 py-2 text-left">{t("audit.table.severity")}</th>
                <th className="px-3 py-2 text-right">{t("audit.table.actions")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {events.map((ev, idx) => {
                const meta = ev.meta
                const sev = ev.event.severity as string | undefined
                const eventId = (ev.event.id ?? ev.event.alertId ?? "-") as string
                const isLegacyFormat = ev.syslog_format == null
                return (
                  <tr key={`${meta.integration_id}-${eventId}-${idx}`}>
                    <td className="px-3 py-2 text-text-secondary">
                      {meta.collected_at ? (
                        <code className="text-xs">{String(meta.collected_at)}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex flex-col gap-0.5">
                        <div className="flex items-center gap-1">
                          <span className="text-text">{meta.vendor ?? meta.platform ?? "?"}</span>
                          <Badge
                            variant="outline"
                            className="ml-2 text-[10px]"
                            title={
                              isLegacyFormat
                                ? t("audit.table.legacyFormatTooltip")
                                : undefined
                            }
                          >
                            {formatLabel(ev)}
                          </Badge>
                        </div>
                        <span className="text-xs text-text-secondary">{meta.event_type ?? meta.stream ?? "?"}</span>
                      </div>
                    </td>
                    <td className="px-3 py-2 text-text">
                      <span
                        className="block max-w-[140px] truncate"
                        title={meta.customer_id != null ? String(meta.customer_id) : undefined}
                      >
                        {meta.customer_id ?? "—"}
                      </span>
                    </td>
                    <td className="px-3 py-2">
                      <code
                        className="block max-w-[200px] truncate text-xs text-text-secondary"
                        title={eventId}
                      >
                        {eventId}
                      </code>
                    </td>
                    <td className="px-3 py-2">
                      {sev ? (
                        <Badge
                          variant={
                            sev.toLowerCase() === "critical"
                              ? "danger"
                              : sev.toLowerCase() === "high"
                                ? "warning"
                                : sev.toLowerCase() === "medium"
                                  ? "primary"
                                  : "outline"
                          }
                          size="sm"
                        >
                          {sev}
                        </Badge>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="xs"
                          variant="ghost"
                          leftIcon={<EyeIcon size={12} />}
                          onClick={() => handleOpenInspect(ev)}
                          title={t("audit.actions.inspectTooltip")}
                        >
                          {t("audit.actions.inspect")}
                        </Button>
                        <Button
                          size="xs"
                          variant="ghost"
                          leftIcon={<CopyIcon size={12} />}
                          onClick={() => void handleCopyJson(ev)}
                          title={t("audit.actions.jsonTooltip")}
                        >
                          {t("audit.actions.json")}
                        </Button>
                        <Button
                          size="xs"
                          variant="outline"
                          leftIcon={<TerminalIcon size={12} />}
                          onClick={() => void handleCopyWire(ev)}
                          title={t("audit.actions.wireTooltip")}
                        >
                          {ev.syslog_format === "rfc3164" ? "RFC 3164" : "RFC 5424"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Modal de inspeção */}
      <Modal
        open={inspected !== null}
        onClose={() => setInspected(null)}
        title={
          inspected
            ? `${inspected.meta.vendor ?? inspected.meta.platform ?? "?"} / ${inspected.meta.event_type ?? inspected.meta.stream ?? "?"}`
            : t("audit.modal.defaultTitle")
        }
        size="lg"
      >
        {inspected && (() => {
          const legacyOcsf = isLegacyOcsfEnvelope(inspected)
          const normalizedJson = !legacyOcsf && inspected.event.normalized != null
            ? JSON.stringify(inspected.event.normalized, null, 2)
            : null
          const rawJson = !legacyOcsf && inspected.event.raw != null
            ? JSON.stringify(inspected.event.raw, null, 2)
            : null

          return (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-2 text-xs">
                <Badge variant="outline">{t("audit.modal.customerBadge", { id: inspected.meta.customer_id ?? "?" })}</Badge>
                <Badge variant="outline">{t("audit.modal.integrationBadge", { id: inspected.meta.integration_id ?? "?" })}</Badge>
                {inspected.meta.collected_at && (
                  <Badge variant="outline">{String(inspected.meta.collected_at)}</Badge>
                )}
                {isLegacyEnvelope(inspected) && (
                  <Badge variant="warning" size="sm">{t("audit.modal.legacyHostnameBadge")}</Badge>
                )}
              </div>

              <Tabs value={inspectTab} onValueChange={setInspectTab}>
                <TabsList ariaLabel={t("audit.modal.tabsAriaLabel")}>
                  <TabsTrigger value="wire">{t("audit.modal.tabWire")}</TabsTrigger>
                  <TabsTrigger value="normalized" disabled={legacyOcsf}>
                    {t("audit.modal.tabNormalized")}
                  </TabsTrigger>
                  <TabsTrigger value="raw" disabled={legacyOcsf}>
                    {t("audit.modal.tabRaw")}
                  </TabsTrigger>
                </TabsList>

                <TabsPanel value="wire">
                  <div className="rounded bg-surface-tertiary p-3">
                    <div className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-text-tertiary">
                      <span>
                        {inspected.syslog_format === "rfc3164"
                          ? t("audit.modal.wireLineLabelRfc3164")
                          : t("audit.modal.wireLineLabelRfc5424")}{" "}
                        {t("audit.modal.wireLineCopyHint", { tool: "wazuh-logtest" })}
                      </span>
                      {!isLegacyEnvelope(inspected) && (
                        <Badge variant="success" size="sm">
                          {t("audit.modal.hostBadge", { hostname: inspected.envelope.hostname })}
                        </Badge>
                      )}
                    </div>
                    <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
                      {buildWireLine(inspected)}
                    </pre>
                    {isLegacyEnvelope(inspected) && (
                      <p className="mt-2 text-xs text-warning-700">
                        {t("audit.modal.legacyEnvelopeNote", { field: "program_name" })}
                      </p>
                    )}
                    {inspected.syslog_format == null && (
                      <p className="mt-2 text-xs text-text-tertiary">
                        {t("audit.modal.legacyFormatNote")}
                      </p>
                    )}
                    <div className="mt-2 flex justify-end">
                      <Button
                        size="xs"
                        variant="outline"
                        leftIcon={<CopyIcon size={12} />}
                        onClick={() => void handleCopyWire(inspected)}
                      >
                        {t("audit.modal.copyLine")}
                      </Button>
                    </div>
                  </div>
                </TabsPanel>

                <TabsPanel value="normalized">
                  <div className="rounded bg-surface-tertiary p-3">
                    <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-text-tertiary">
                      {t("audit.modal.normalizedTitle")}
                    </div>
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
                      {normalizedJson ?? "—"}
                    </pre>
                  </div>
                </TabsPanel>

                <TabsPanel value="raw">
                  <div className="rounded bg-surface-tertiary p-3">
                    <div className="mb-1 text-xs font-semibold uppercase tracking-wider text-text-tertiary">
                      {t("audit.modal.rawTitle")}
                    </div>
                    <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
                      {rawJson ?? "—"}
                    </pre>
                    <div className="mt-2 flex justify-end">
                      <Button
                        size="xs"
                        variant="outline"
                        leftIcon={<CopyIcon size={12} />}
                        onClick={() => void handleCopyJson(inspected)}
                      >
                        {t("audit.modal.copyJson")}
                      </Button>
                    </div>
                  </div>
                </TabsPanel>
              </Tabs>
            </div>
          )
        })()}
      </Modal>

      <ConfirmDialog
        open={confirmClear}
        title={t("audit.clearDialog.title")}
        description={
          <>
            {t("audit.clearDialog.descriptionPrefix")}{" "}
            <strong>{t("audit.clearDialog.descriptionAllEvents", { count: events.length })}</strong>{" "}
            {t("audit.clearDialog.descriptionSuffix")}
          </>
        }
        confirmLabel={t("audit.clearDialog.confirm")}
        confirmVariant="danger"
        loading={clearing}
        onConfirm={() => void handleClear()}
        onClose={() => setConfirmClear(false)}
      />
    </div>
  )
}

export default CollectorAuditPanel
