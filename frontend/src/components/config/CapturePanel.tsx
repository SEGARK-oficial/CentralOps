"use client"

import type React from "react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Trans, useTranslation } from "react-i18next"
import {
  CopyIcon,
  EyeIcon,
  PlayIcon,
  RadioIcon,
  RefreshCwIcon,
  SquareIcon,
  Trash2Icon,
} from "lucide-react"
import * as api from "@/services/api"
import { ApiRequestError } from "@/services/api"
import type { CaptureEvent, CaptureSession } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Modal } from "@/components/ui/Modal/Modal"
import { Notice } from "@/components/ui/Notice/Notice"

// Opções de duração da janela de captura (alinhadas ao backend: 1s–3600s).
const DURATION_OPTIONS: Array<{ value: number; labelKey: string }> = [
  { value: 60, labelKey: "capture.durations.1m" },
  { value: 300, labelKey: "capture.durations.5m" },
  { value: 900, labelKey: "capture.durations.15m" },
  { value: 1800, labelKey: "capture.durations.30m" },
  { value: 3600, labelKey: "capture.durations.1h" },
]

// Tamanho do ring (quantos eventos a sessão retém). Alinhado ao backend (1–20000).
const RING_OPTIONS = [1000, 5000, 10000, 20000]

// Cadência do polling enquanto há sessão ativa (sessões + eventos da selecionada).
const POLL_INTERVAL_MS = 3000

/** Converte epoch-seconds em hora local legível (== null, não falsy: epoch 0 é válido). */
function formatEpoch(seconds?: number | null): string {
  if (seconds == null) return "—"
  return new Date(seconds * 1000).toLocaleString()
}

/** Segundos restantes até expirar (>=0), ou null se sem expiração. */
function remainingSeconds(expiresAt?: number | null): number | null {
  if (expiresAt == null) return null
  return Math.max(0, Math.round(expiresAt - Date.now() / 1000))
}

function statusVariant(status: string): "success" | "outline" | "warning" {
  if (status === "active") return "success"
  if (status === "expired") return "warning"
  return "outline"
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

export const CapturePanel: React.FC = () => {
  const { t } = useTranslation("config")
  const [vendor, setVendor] = useState<string>("")
  const [duration, setDuration] = useState<number>(300)
  const [ringSize, setRingSize] = useState<number>(5000)
  const [starting, setStarting] = useState(false)

  const [sessions, setSessions] = useState<CaptureSession[]>([])
  const [loadingSessions, setLoadingSessions] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [events, setEvents] = useState<CaptureEvent[]>([])
  const [loadingEvents, setLoadingEvents] = useState(false)

  const [busyId, setBusyId] = useState<string | null>(null) // stop/delete em andamento
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [inspected, setInspected] = useState<CaptureEvent | null>(null)
  const [vendorCatalog, setVendorCatalog] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<
    { type: "success" | "error"; message: string } | null
  >(null)

  // Guard de unmount: evita setState em componente desmontado (a request em
  // voo pode resolver depois que o usuário trocou de aba).
  const mountedRef = useRef(true)
  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])
  // Guard de poll: pula o tick se o anterior ainda não resolveu (numa conexão
  // lenta, intervalos de 3s acumulariam requests concorrentes que se
  // sobrescrevem em ordem não-determinística).
  const pollingRef = useRef(false)

  // ``silent`` (usado pelo poll) não mexe no spinner — senão o botão
  // "Atualizar" piscaria a cada 3s.
  const loadSessions = useCallback(async (opts?: { silent?: boolean }) => {
    if (!opts?.silent) setLoadingSessions(true)
    try {
      const data = await api.listCaptureSessions()
      if (!mountedRef.current) return
      setSessions(data.sessions)
      setError(null)
    } catch (err) {
      if (mountedRef.current)
        setError(err instanceof Error ? err.message : t("capture.loadSessionsError"))
    } finally {
      if (mountedRef.current) setLoadingSessions(false)
    }
  }, [t])

  const loadEvents = useCallback(async (sessionId: string, opts?: { silent?: boolean }) => {
    if (!opts?.silent) setLoadingEvents(true)
    try {
      const data = await api.getCaptureEvents(sessionId, 500)
      if (!mountedRef.current) return
      setEvents(data.events)
    } catch (err) {
      if (mountedRef.current)
        setError(err instanceof Error ? err.message : t("capture.loadEventsError"))
    } finally {
      if (mountedRef.current) setLoadingEvents(false)
    }
  }, [t])

  // Carga inicial: sessões + catálogo de vendors (para o select de escopo).
  useEffect(() => {
    void loadSessions()
  }, [loadSessions])

  useEffect(() => {
    let cancelled = false
    api
      .listPlatformsStreams()
      .then((resp) => {
        if (!cancelled) setVendorCatalog(Object.keys(resp.platforms ?? {}).sort())
      })
      .catch(() => {
        /* não-fatal — o select fica só com "Todos os vendors" */
      })
    return () => {
      cancelled = true
    }
  }, [])

  const hasActive = useMemo(() => sessions.some((s) => s.status === "active"), [sessions])
  const selected = useMemo(
    () => sessions.find((s) => s.id === selectedId) ?? null,
    [sessions, selectedId],
  )

  // Polling: enquanto houver sessão ativa, atualiza a lista; se a sessão
  // selecionada estiver ativa, atualiza também os eventos. Para quando nada
  // está ativo (evita bater no backend à toa). ``silent`` p/ não piscar os
  // spinners; ``pollingRef`` evita ticks concorrentes sobrepostos.
  useEffect(() => {
    if (!hasActive) return
    const handle = window.setInterval(() => {
      if (pollingRef.current) return
      pollingRef.current = true
      const tasks = [loadSessions({ silent: true })]
      if (selectedId && selected?.status === "active") {
        tasks.push(loadEvents(selectedId, { silent: true }))
      }
      void Promise.all(tasks).finally(() => {
        pollingRef.current = false
      })
    }, POLL_INTERVAL_MS)
    return () => window.clearInterval(handle)
  }, [hasActive, selectedId, selected?.status, loadSessions, loadEvents])

  // Se a sessão selecionada some do poll (expirou e saiu da listagem, ou foi
  // excluída por outro caminho), limpa a seleção pendente e os eventos stale.
  useEffect(() => {
    if (selectedId && !selected) {
      setSelectedId(null)
      setEvents([])
    }
  }, [selectedId, selected])

  const handleStart = async () => {
    try {
      setStarting(true)
      setFeedback(null)
      const session = await api.startCaptureSession({
        vendor: vendor || undefined,
        duration_seconds: duration,
        ring_size: ringSize,
      })
      setFeedback({
        type: "success",
        message: t("capture.startSuccess", {
          vendorSuffix: session.vendor ? t("capture.startVendorSuffix", { vendor: session.vendor }) : "",
        }),
      })
      setSelectedId(session.id)
      setEvents([])
      await loadSessions()
    } catch (err) {
      const isLimit = err instanceof ApiRequestError && err.statusCode === 429
      setFeedback({
        type: "error",
        message: isLimit
          ? t("capture.limitReached")
          : err instanceof Error
            ? err.message
            : t("capture.startError"),
      })
    } finally {
      setStarting(false)
    }
  }

  const handleSelect = (sessionId: string) => {
    setSelectedId(sessionId)
    void loadEvents(sessionId)
  }

  const handleStop = async (sessionId: string) => {
    try {
      setBusyId(sessionId)
      await api.stopCaptureSession(sessionId)
      setFeedback({ type: "success", message: t("capture.stopSuccess") })
      await loadSessions()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("capture.stopError") })
    } finally {
      setBusyId(null)
    }
  }

  const handleDelete = async (sessionId: string) => {
    try {
      setBusyId(sessionId)
      await api.deleteCaptureSession(sessionId)
      if (selectedId === sessionId) {
        setSelectedId(null)
        setEvents([])
      }
      setConfirmDelete(null)
      setFeedback({ type: "success", message: t("capture.deleteSuccess") })
      await loadSessions()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : t("capture.deleteError") })
    } finally {
      setBusyId(null)
    }
  }

  const handleCopyJson = async (ev: CaptureEvent) => {
    await copyToClipboard(JSON.stringify(ev.event, null, 2))
    setFeedback({ type: "success", message: t("capture.copyJsonSuccess") })
  }

  return (
    <div className="space-y-4">
      <Notice variant="info" title={t("capture.intro.title")}>
        <Trans i18nKey="capture.intro.body" t={t} components={{ strong: <strong /> }} />
      </Notice>

      {feedback && (
        <Notice variant={feedback.type === "success" ? "success" : "danger"}>
          {feedback.message}
        </Notice>
      )}
      {error && <Notice variant="danger">{error}</Notice>}

      {/* Formulário de início */}
      <div className="flex flex-wrap items-end gap-3 rounded-md border border-border bg-surface p-3">
        <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
          {t("capture.form.vendorScope")}
          <select
            className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
            value={vendor}
            onChange={(e) => setVendor(e.target.value)}
            aria-label={t("capture.form.vendorAriaLabel")}
          >
            <option value="">{t("capture.form.allVendors")}</option>
            {vendorCatalog.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
          {t("capture.form.duration")}
          <select
            className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
            value={duration}
            onChange={(e) => setDuration(Number(e.target.value))}
            aria-label={t("capture.form.durationAriaLabel")}
          >
            {DURATION_OPTIONS.map((d) => (
              <option key={d.value} value={d.value}>
                {t(d.labelKey)}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-text-secondary">
          {t("capture.form.bufferSize")}
          <select
            className="h-8 rounded border border-border bg-surface px-2 text-sm text-text"
            value={ringSize}
            onChange={(e) => setRingSize(Number(e.target.value))}
            aria-label={t("capture.form.bufferSizeAriaLabel")}
          >
            {RING_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {t("capture.form.eventsUnit", { count: n })}
              </option>
            ))}
          </select>
        </label>
        <Button
          variant="primary"
          size="sm"
          leftIcon={<PlayIcon size={14} />}
          onClick={() => void handleStart()}
          loading={starting}
        >
          {t("capture.form.start")}
        </Button>
        <div className="ml-auto">
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCwIcon size={14} />}
            onClick={() => void loadSessions()}
            loading={loadingSessions}
          >
            {t("capture.form.refresh")}
          </Button>
        </div>
      </div>

      {/* Lista de sessões */}
      {loadingSessions && sessions.length === 0 ? (
        <div className="flex justify-center py-10">
          <LoadingSpinner size="md" text={t("capture.sessionsLoading")} />
        </div>
      ) : sessions.length === 0 ? (
        <EmptyState
          icon={<RadioIcon size={32} />}
          title={t("capture.sessionsEmptyTitle")}
          description={t("capture.sessionsEmptyDescription")}
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
              <tr>
                <th className="px-3 py-2 text-left">{t("capture.table.vendor")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.status")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.events")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.started")}</th>
                <th className="px-3 py-2 text-left">{t("capture.table.expires")}</th>
                <th className="px-3 py-2 text-right">{t("capture.table.actions")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {sessions.map((s) => {
                const remain = remainingSeconds(s.expires_at)
                const isSelected = s.id === selectedId
                return (
                  <tr
                    key={s.id}
                    className={isSelected ? "bg-primary-50/40 dark:bg-primary-900/10" : undefined}
                  >
                    <td className="px-3 py-2 text-text">{s.vendor ?? t("capture.table.allVendors")}</td>
                    <td className="px-3 py-2">
                      <Badge variant={statusVariant(s.status)} size="sm">
                        {s.status}
                      </Badge>
                    </td>
                    <td className="px-3 py-2 text-text">{s.event_count}</td>
                    <td className="px-3 py-2 text-text-secondary">
                      <code className="text-xs">{formatEpoch(s.created_at)}</code>
                    </td>
                    <td className="px-3 py-2 text-text-secondary">
                      {s.status === "active" && remain != null ? (
                        <span className="text-xs">{t("capture.table.expiresIn", { seconds: remain })}</span>
                      ) : (
                        <code className="text-xs">{formatEpoch(s.expires_at)}</code>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      <div className="flex justify-end gap-1">
                        <Button
                          size="xs"
                          variant="ghost"
                          leftIcon={<EyeIcon size={12} />}
                          onClick={() => handleSelect(s.id)}
                          title={t("capture.table.viewEventsTooltip")}
                        >
                          {t("capture.table.viewEvents")}
                        </Button>
                        {s.status === "active" && (
                          <Button
                            size="xs"
                            variant="outline"
                            leftIcon={<SquareIcon size={12} />}
                            onClick={() => void handleStop(s.id)}
                            loading={busyId === s.id}
                            title={t("capture.table.stopTooltip")}
                          >
                            {t("capture.table.stop")}
                          </Button>
                        )}
                        <Button
                          size="xs"
                          variant="ghost"
                          leftIcon={<Trash2Icon size={12} />}
                          onClick={() => setConfirmDelete(s.id)}
                          disabled={busyId === s.id}
                          title={t("capture.table.deleteTooltip")}
                        >
                          {t("capture.table.delete")}
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

      {/* Eventos da sessão selecionada */}
      {selected && (
        <div className="space-y-2 rounded-md border border-border p-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 text-sm font-semibold text-text">
              <RadioIcon size={16} className="text-primary-600" />
              {t("capture.events.title", { vendor: selected.vendor ?? t("capture.events.allVendors") })}
              <Badge variant={statusVariant(selected.status)} size="sm">
                {selected.status}
              </Badge>
              {selected.status === "active" && (
                <span className="text-xs font-normal text-text-tertiary">
                  {t("capture.events.liveUpdating")}
                </span>
              )}
            </div>
            <Button
              size="xs"
              variant="ghost"
              leftIcon={<RefreshCwIcon size={12} />}
              onClick={() => void loadEvents(selected.id)}
              loading={loadingEvents}
            >
              {t("capture.events.refresh")}
            </Button>
          </div>

          {loadingEvents && events.length === 0 ? (
            <div className="flex justify-center py-6">
              <LoadingSpinner size="sm" text={t("capture.events.loading")} />
            </div>
          ) : events.length === 0 ? (
            <EmptyState
              icon={<RadioIcon size={28} />}
              title={selected.status === "active" ? t("capture.events.waitingTitle") : t("capture.events.emptyTitle")}
              description={
                selected.status === "active"
                  ? t("capture.events.waitingDescription")
                  : t("capture.events.emptyDescription")
              }
            />
          ) : (
            <div className="overflow-x-auto rounded border border-border">
              <table className="w-full text-sm">
                <thead className="bg-surface-tertiary text-xs uppercase tracking-wider text-text-secondary">
                  <tr>
                    <th className="px-3 py-2 text-left">{t("capture.events.table.capturedAt")}</th>
                    <th className="px-3 py-2 text-left">{t("capture.events.table.vendor")}</th>
                    <th className="px-3 py-2 text-left">{t("capture.events.table.preview")}</th>
                    <th className="px-3 py-2 text-right">{t("capture.events.table.actions")}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {events.map((ev, idx) => (
                    <tr key={`${selected.id}-${ev.captured_at ?? idx}-${ev.vendor ?? ""}-${idx}`}>
                      <td className="px-3 py-2 text-text-secondary">
                        <code className="text-xs">{formatEpoch(ev.captured_at)}</code>
                      </td>
                      <td className="px-3 py-2 text-text">{ev.vendor ?? "—"}</td>
                      <td className="px-3 py-2">
                        <code className="block max-w-[420px] truncate text-xs text-text-secondary">
                          {JSON.stringify(ev.event)}
                        </code>
                      </td>
                      <td className="px-3 py-2">
                        <div className="flex justify-end gap-1">
                          <Button
                            size="xs"
                            variant="ghost"
                            leftIcon={<EyeIcon size={12} />}
                            onClick={() => setInspected(ev)}
                            title={t("capture.events.inspectTooltip")}
                          >
                            {t("capture.events.inspect")}
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            leftIcon={<CopyIcon size={12} />}
                            onClick={() => void handleCopyJson(ev)}
                            title={t("capture.events.jsonTooltip")}
                          >
                            {t("capture.events.json")}
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Modal de inspeção */}
      <Modal
        open={inspected !== null}
        onClose={() => setInspected(null)}
        title={inspected ? t("capture.inspectModal.title", { vendor: inspected.vendor ?? "?" }) : t("capture.inspectModal.defaultTitle")}
        size="lg"
      >
        {inspected && (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2 text-xs">
              {inspected.vendor && <Badge variant="outline">{inspected.vendor}</Badge>}
              {inspected.captured_at && (
                <Badge variant="outline">{formatEpoch(inspected.captured_at)}</Badge>
              )}
            </div>
            <div className="rounded bg-surface-tertiary p-3">
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs text-text">
                {JSON.stringify(inspected.event, null, 2)}
              </pre>
              <div className="mt-2 flex justify-end">
                <Button
                  size="xs"
                  variant="outline"
                  leftIcon={<CopyIcon size={12} />}
                  onClick={() => void handleCopyJson(inspected)}
                >
                  {t("capture.inspectModal.copyJson")}
                </Button>
              </div>
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("capture.deleteDialog.title")}
        description={t("capture.deleteDialog.description")}
        confirmLabel={t("capture.deleteDialog.confirm")}
        confirmVariant="danger"
        loading={busyId === confirmDelete}
        onConfirm={() => confirmDelete && void handleDelete(confirmDelete)}
        onClose={() => setConfirmDelete(null)}
      />
    </div>
  )
}

export default CapturePanel
