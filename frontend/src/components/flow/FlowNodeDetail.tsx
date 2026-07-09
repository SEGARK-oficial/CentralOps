/**
 * FlowNodeDetail — painel lateral (drawer) de detalhes de um nó do grafo de fluxo.
 *
 * Acessibilidade:
 * - role="dialog" aria-modal
 * - ESC fecha; foco gerenciado (primeiro elemento focável ao abrir; retorno ao trigger ao fechar)
 * - Tab trap dentro do painel
 *
 * Para destinos: exibe bytes_per_min + últimos eventos via getDestinationTap.
 */
import type React from "react"
import { useEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import { useTranslation } from "react-i18next"
import { XIcon, ActivityIcon, DatabaseIcon, NetworkIcon, ServerIcon } from "lucide-react"
import * as api from "@/services/api"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { fmtRate } from "@/lib/fmt"
import { formatBytes, formatRelativeDate } from "@/lib/utils"
import type { FlowNodeId } from "./FlowCanvas"
import type { DestinationTap } from "@/types"

interface FlowNodeDetailProps {
  node: FlowNodeId | null
  onClose: () => void
}

type StatusVariant = "success" | "warning" | "danger" | "default"

function statusVariant(s: string): StatusVariant {
  if (s === "healthy") return "success"
  if (s === "degraded") return "warning"
  if (s === "unhealthy") return "danger"
  return "default"
}

function statusLabelKey(s: string): string {
  const map: Record<string, string> = {
    healthy: "flow.nodeDetail.status.healthy",
    degraded: "flow.nodeDetail.status.degraded",
    unhealthy: "flow.nodeDetail.status.unhealthy",
    unknown: "flow.nodeDetail.status.unknown",
  }
  return map[s] ?? ""
}

const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

export const FlowNodeDetail: React.FC<FlowNodeDetailProps> = ({ node, onClose }) => {
  const { t } = useTranslation("dashboard")
  const statusLabel = (s: string): string => {
    const key = statusLabelKey(s)
    return key ? t(key) : s
  }
  const panelRef = useRef<HTMLDivElement>(null)
  const previousFocus = useRef<HTMLElement | null>(null)
  const [tap, setTap] = useState<DestinationTap | null>(null)
  const [tapLoading, setTapLoading] = useState(false)
  const [tapError, setTapError] = useState<string | null>(null)

  const open = node !== null

  // Focus management
  useEffect(() => {
    if (!open) return
    previousFocus.current = document.activeElement as HTMLElement
    const timer = window.setTimeout(() => {
      const panel = panelRef.current
      if (!panel) return
      const focusable = panel.querySelectorAll<HTMLElement>(FOCUSABLE)
      const first = focusable[0]
      if (first) first.focus()
      else panel.focus()
    }, 50)

    document.body.style.overflow = "hidden"

    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault()
        onClose()
        return
      }
      if (e.key !== "Tab") return
      const panel = panelRef.current
      if (!panel) return
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      )
      if (focusable.length === 0) {
        e.preventDefault()
        panel.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          e.preventDefault()
          last.focus()
        }
      } else if (active === last || !panel.contains(active)) {
        e.preventDefault()
        first.focus()
      }
    }

    document.addEventListener("keydown", handleKey)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener("keydown", handleKey)
      document.body.style.overflow = ""
      previousFocus.current?.focus()
    }
  }, [open, onClose])

  // Load tap data for destination nodes
  useEffect(() => {
    if (!open || node?.kind !== "dest") {
      setTap(null)
      setTapError(null)
      return
    }
    const destId = node.node.id
    setTapLoading(true)
    setTapError(null)
    let cancelled = false
    api
      .getDestinationTap(destId, { limit: 20 })
      .then((res) => {
        if (!cancelled) setTap(res)
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setTapError(err instanceof Error ? err.message : t("flow.nodeDetail.loadEventsError"))
      })
      .finally(() => {
        if (!cancelled) setTapLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [open, node, t])

  if (!open || !node) return null

  // ── Render helpers ──────────────────────────────────────────────────────
  const renderSourceDetail = () => {
    if (node.kind !== "source") return null
    const s = node.node
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={statusVariant(s.status)} size="sm" dot>
            {statusLabel(s.status)}
          </Badge>
          <Badge variant="outline" size="sm">{s.platform}</Badge>
        </div>
        <Card padding="sm" className="space-y-3">
          <Row label={t("flow.nodeDetail.row.type")} value={t("flow.nodeDetail.kind.source")} icon={<ServerIcon size={13} />} />
          <Row label={t("flow.nodeDetail.row.platform")} value={s.platform} />
          {/* Unidade unificada do flow-view: eventos/min (sem EPS). */}
          <Row label={t("flow.nodeDetail.row.eventsPerMinute")} value={`${fmtRate(s.events_per_minute)}/min`} />
          <Row label={t("flow.nodeDetail.row.status")} value={statusLabel(s.status)} />
        </Card>
      </div>
    )
  }

  const renderRouteDetail = () => {
    if (node.kind !== "route") return null
    const r = node.node
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={r.enabled ? "primary" : "default"} size="sm">
            {r.enabled ? t("flow.nodeDetail.active") : t("flow.nodeDetail.inactive")}
          </Badge>
          {r.is_system && <Badge variant="outline" size="sm">{t("flow.nodeDetail.system")}</Badge>}
          {r.action === "drop" && <Badge variant="danger" size="sm">{t("flow.nodeDetail.drop")}</Badge>}
        </div>
        <Card padding="sm" className="space-y-3">
          <Row label={t("flow.nodeDetail.row.type")} value={t("flow.nodeDetail.kind.route")} icon={<NetworkIcon size={13} />} />
          <Row label={t("flow.nodeDetail.row.action")} value={r.action} />
          <Row label={t("flow.nodeDetail.row.routedPerMinute")} value={`${fmtRate(r.routed_per_min)}/min`} />
          <Row label={t("flow.nodeDetail.row.matchedPerMinute")} value={`${fmtRate(r.matched_per_min)}/min`} />
          {r.drop_per_min > 0 && (
            <Row label={t("flow.nodeDetail.row.droppedPerMinute")} value={`${fmtRate(r.drop_per_min)}/min`} className="text-danger-600" />
          )}
          {r.destination_ids.length > 0 && (
            <div>
              <span className="text-xs font-medium text-text-secondary">{t("flow.nodeDetail.destinations")}</span>
              <div className="mt-1 flex flex-wrap gap-1">
                {r.destination_ids.map((id) => (
                  <Badge key={id} variant="outline" size="sm">{id}</Badge>
                ))}
              </div>
            </div>
          )}
        </Card>
      </div>
    )
  }

  const renderDestDetail = () => {
    if (node.kind !== "dest") return null
    const d = node.node
    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={statusVariant(d.status)} size="sm" dot>
            {statusLabel(d.status)}
          </Badge>
          <Badge variant="outline" size="sm">{d.kind}</Badge>
        </div>
        <Card padding="sm" className="space-y-3">
          <Row label={t("flow.nodeDetail.row.type")} value={t("flow.nodeDetail.kind.destination")} icon={<DatabaseIcon size={13} />} />
          <Row label={t("flow.nodeDetail.row.kind")} value={d.kind} />
          {d.eps != null && <Row label={t("flow.nodeDetail.row.deliveredPerMinute")} value={`${fmtRate(d.eps * 60)}/min`} />}
          {d.bytes_per_min != null && (
            <Row label={t("flow.nodeDetail.row.throughput")} value={`${formatBytes(d.bytes_per_min)}/min`} />
          )}
          <Row label={t("flow.nodeDetail.row.status")} value={statusLabel(d.status)} />
        </Card>

        {/* Live tap */}
        <div>
          <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-text-secondary">
            <ActivityIcon size={12} />
            {t("flow.nodeDetail.recentEvents")}
          </h4>
          {tapLoading && (
            <p className="text-xs text-text-tertiary">{t("flow.nodeDetail.loadingEvents")}</p>
          )}
          {tapError && (
            <p className="text-xs text-danger-600">{tapError}</p>
          )}
          {!tapLoading && !tapError && tap && tap.entries.length === 0 && (
            <p className="text-xs text-text-tertiary">{t("flow.nodeDetail.noRecentEvents")}</p>
          )}
          {!tapLoading && !tapError && tap && tap.entries.length > 0 && (
            <ul className="space-y-1.5">
              {tap.entries.slice(0, 20).map((entry, i) => {
                const ts = entry.timestamp as string | undefined
                const summary =
                  (entry.event_type as string) ??
                  (entry.type as string) ??
                  (entry.action as string) ??
                  t("flow.liveFeed.genericEvent")
                const redacted =
                  (entry._redacted as boolean) ?? false
                return (
                  <li
                    key={i}
                    className="rounded-md border border-border bg-surface-tertiary/50 px-2.5 py-1.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-xs font-medium text-text">
                        {String(summary)}
                      </span>
                      {redacted && (
                        <Badge variant="warning" size="sm">{t("flow.nodeDetail.redacted")}</Badge>
                      )}
                    </div>
                    {ts && (
                      <span className="mt-0.5 block text-[10px] text-text-tertiary">
                        {formatRelativeDate(ts)}
                      </span>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    )
  }

  const title =
    node.kind === "source"
      ? node.node.name
      : node.kind === "route"
        ? (node.node.is_system ? t("flow.canvas.catchAll") : node.node.name)
        : node.node.name

  const kindLabel =
    node.kind === "source" ? t("flow.nodeDetail.kind.source") : node.kind === "route" ? t("flow.nodeDetail.kind.route") : t("flow.nodeDetail.kind.destination")

  return createPortal(
    <div className="fixed inset-0 z-[1040]">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/40"
        onClick={onClose}
        aria-hidden="true"
      />
      {/* Panel */}
      <div
        ref={panelRef}
        className="absolute inset-y-0 right-0 flex w-full flex-col overflow-hidden border-l border-border bg-surface shadow-xl sm:max-w-md"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={t("flow.nodeDetail.detailAriaLabel", { title })}
        tabIndex={-1}
      >
        {/* Header */}
        <div className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wider text-text-tertiary">
              {kindLabel}
            </p>
            <h2 className="mt-0.5 truncate text-lg font-semibold text-text">{title}</h2>
          </div>
          <Button variant="ghost" size="xs" onClick={onClose} aria-label={t("flow.nodeDetail.closeAriaLabel")}>
            <XIcon size={16} />
          </Button>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {renderSourceDetail()}
          {renderRouteDetail()}
          {renderDestDetail()}
        </div>
      </div>
    </div>,
    document.body,
  )
}

// ── Row helper ─────────────────────────────────────────────────────────────
interface RowProps {
  label: string
  value: string
  icon?: React.ReactNode
  className?: string
}
const Row: React.FC<RowProps> = ({ label, value, icon, className }) => (
  <div className="flex items-center justify-between gap-2 text-sm">
    <span className="flex items-center gap-1 text-xs text-text-secondary">
      {icon}
      {label}
    </span>
    <span className={`font-medium text-text ${className ?? ""}`}>{value}</span>
  </div>
)
