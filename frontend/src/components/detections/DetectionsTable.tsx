"use client"

import type React from "react"
import { useMemo, useRef } from "react"
import { useTranslation } from "react-i18next"
import { useVirtualizer } from "@tanstack/react-virtual"
import { ShieldAlertIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import LoadingSpinner from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { detectionSeverityEncoding } from "@/lib/severity"
import type { DetectionRead, DetectionSource, DetectionStatus } from "@/types"
import { formatDate } from "@/lib/utils"

interface DetectionsTableProps {
  detections: DetectionRead[]
  loading?: boolean
  onRowClick: (detection: DetectionRead) => void
}

// PERF-08-alike: acima disto a tabela feita à mão virtualiza (useDetections
// pede até 200 linhas por vez). Abaixo, renderiza tudo — jsdom não tem layout
// real e o virtualizer devolve 0 itens sem um container com altura de
// verdade, então manter o caminho simples para listas pequenas também evita
// depender de mock nos testes que já cobrem o comportamento (poucas linhas).
const VIRTUALIZE_THRESHOLD = 60
const ROW_HEIGHT_PX = 57
const MAX_HEIGHT_PX = 560

// ── Helpers ──────────────────────────────────────────────────────────────────

type TFn = ReturnType<typeof useTranslation>["t"]

function sourceLabelKey(source: DetectionSource): string {
  switch (source) {
    case "scheduled_query":
      return "schedules:detections.source.scheduled_query"
    case "live_query":
      return "schedules:detections.source.live_query"
    case "correlation":
      return "schedules:detections.source.correlation"
    default:
      return source
  }
}

function statusBadgeVariant(status: DetectionStatus): "default" | "warning" | "success" {
  switch (status) {
    case "open":
      return "warning"
    case "ack":
      return "default"
    case "closed":
      return "success"
    default:
      return "default"
  }
}

function statusLabelKey(status: DetectionStatus): string {
  switch (status) {
    case "open":
      return "schedules:detections.statusSingular.open"
    case "ack":
      return "schedules:detections.statusSingular.ack"
    case "closed":
      return "schedules:detections.statusSingular.closed"
    default:
      return status
  }
}

// ── Classes ───────────────────────────────────────────────────────────────────

const thCls = "px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary"
const tdCls = "px-4 py-3 text-sm align-top"

function DetectionSeverityBadge({ severityId }: { severityId: number }) {
  const { t } = useTranslation()
  const enc = detectionSeverityEncoding(severityId)
  return (
    <Badge variant={enc.badgeVariant} size="sm">
      {t(enc.labelKey, enc.labelParams)}
    </Badge>
  )
}

function DetectionRuleButton({ detection, t, onRowClick }: { detection: DetectionRead; t: TFn; onRowClick: (d: DetectionRead) => void }) {
  return (
    // A11Y-14: a linha volta a ser um <tr> comum (a semântica de linha de
    // tabela não é apagada por um role de botão que não existe em HTML), e só
    // a célula da regra — que é o alvo natural do clique — vira <button>.
    // `focus-ring`: mesma estratégia de foco do design system em toda a app;
    // o anel antigo (`ring-primary-500/40` sobre o `<tr>`) dava 2.03:1.
    <button
      type="button"
      className="max-w-[280px] space-y-0.5 rounded text-left focus-ring"
      aria-label={t("schedules:detections.table.rowAriaLabel", { name: detection.rule_name || detection.dedup_key })}
      onClick={() => onRowClick(detection)}
    >
      <div className="truncate font-medium text-text" title={detection.rule_name ?? undefined}>
        {detection.rule_name || "-"}
      </div>
      {detection.rule_id && <div className="font-mono text-xs text-text-tertiary">{detection.rule_id}</div>}
    </button>
  )
}

function DetectionRowCells({ detection, t, onRowClick }: { detection: DetectionRead; t: TFn; onRowClick: (d: DetectionRead) => void }) {
  return (
    <>
      <td className={tdCls}>
        <DetectionSeverityBadge severityId={detection.severity_id} />
      </td>
      <td className={tdCls}>
        <span className="text-text-secondary">{t(sourceLabelKey(detection.source))}</span>
      </td>
      <td className={tdCls}>
        <DetectionRuleButton detection={detection} t={t} onRowClick={onRowClick} />
      </td>
      <td className={tdCls}>
        <Badge variant={statusBadgeVariant(detection.status)} size="sm">
          {t(statusLabelKey(detection.status))}
        </Badge>
      </td>
      <td className={`${tdCls} text-right font-mono font-semibold tabular-nums text-text`}>{detection.count ?? 1}</td>
      <td className={`${tdCls} whitespace-nowrap text-xs text-text-secondary`}>
        {detection.last_seen ? formatDate(detection.last_seen) : "-"}
      </td>
    </>
  )
}

// ── Component ─────────────────────────────────────────────────────────────────

export const DetectionsTable: React.FC<DetectionsTableProps> = ({ detections, loading = false, onRowClick }) => {
  const { t } = useTranslation("schedules")
  const scrollRef = useRef<HTMLDivElement>(null)
  const virtualize = detections.length > VIRTUALIZE_THRESHOLD

  const rowVirtualizer = useVirtualizer({
    count: virtualize ? detections.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT_PX,
    overscan: 8,
  })
  const virtualItems = virtualize ? rowVirtualizer.getVirtualItems() : []
  const paddingTop = virtualItems.length > 0 ? virtualItems[0].start : 0
  const paddingBottom =
    virtualItems.length > 0 ? rowVirtualizer.getTotalSize() - virtualItems[virtualItems.length - 1].end : 0

  const columns = useMemo(
    () => [
      { key: "severity", label: t("schedules:detections.table.columns.severity"), nowrap: true },
      { key: "source", label: t("schedules:detections.table.columns.source"), nowrap: false },
      { key: "rule", label: t("schedules:detections.table.columns.rule"), nowrap: false },
      { key: "status", label: t("schedules:detections.table.columns.status"), nowrap: true },
      { key: "occurrences", label: t("schedules:detections.table.columns.occurrences"), nowrap: true, right: true },
      { key: "lastSeen", label: t("schedules:detections.table.columns.lastSeen"), nowrap: true },
    ],
    [t],
  )

  if (loading) {
    return (
      <div className="flex min-h-[240px] items-center justify-center">
        <LoadingSpinner size="lg" text={t("schedules:detections.table.loading")} />
      </div>
    )
  }

  if (detections.length === 0) {
    return (
      <EmptyState
        icon={<ShieldAlertIcon size={48} />}
        title={t("schedules:detections.table.emptyTitle")}
        description={t("schedules:detections.table.emptyDescription")}
      />
    )
  }

  return (
    <div className="space-y-4">
      {/* Desktop: tabela com rolagem horizontal segura */}
      <div className="hidden overflow-hidden rounded-xl border border-border md:block">
        <div
          ref={scrollRef}
          className="overflow-x-auto"
          style={virtualize ? { maxHeight: MAX_HEIGHT_PX, overflowY: "auto" } : undefined}
        >
          <table
            className="w-full min-w-[860px] text-sm"
            role="table"
            aria-label={t("schedules:detections.table.ariaLabel")}
            aria-rowcount={detections.length + 1}
          >
            <thead className="sticky top-0 z-10 bg-surface-tertiary">
              <tr className="border-b border-border">
                {columns.map((col) => (
                  <th
                    key={col.key}
                    scope="col"
                    className={`${thCls} ${col.nowrap ? "whitespace-nowrap" : ""} ${col.right ? "text-right" : ""}`}
                  >
                    {col.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-border bg-surface">
              {virtualize ? (
                <>
                  {paddingTop > 0 && (
                    <tr aria-hidden="true" style={{ height: paddingTop }}>
                      <td colSpan={columns.length} className="border-0 p-0" />
                    </tr>
                  )}
                  {virtualItems.map((virtualRow) => {
                    const detection = detections[virtualRow.index]
                    return (
                      <tr
                        key={detection.id}
                        data-index={virtualRow.index}
                        ref={rowVirtualizer.measureElement}
                        aria-rowindex={virtualRow.index + 2}
                        className="transition-colors hover:bg-surface-tertiary/40"
                      >
                        <DetectionRowCells detection={detection} t={t} onRowClick={onRowClick} />
                      </tr>
                    )
                  })}
                  {paddingBottom > 0 && (
                    <tr aria-hidden="true" style={{ height: paddingBottom }}>
                      <td colSpan={columns.length} className="border-0 p-0" />
                    </tr>
                  )}
                </>
              ) : (
                detections.map((detection, index) => (
                  <tr
                    key={detection.id}
                    aria-rowindex={index + 2}
                    className="transition-colors hover:bg-surface-tertiary/40"
                  >
                    <DetectionRowCells detection={detection} t={t} onRowClick={onRowClick} />
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Mobile: cartões */}
      <div className="space-y-3 md:hidden">
        {detections.map((detection) => (
          <button
            key={detection.id}
            type="button"
            className="w-full rounded-xl border border-border bg-surface p-4 text-left transition-colors hover:bg-surface-tertiary/40 focus-ring"
            aria-label={t("schedules:detections.table.rowAriaLabel", { name: detection.rule_name || detection.dedup_key })}
            onClick={() => onRowClick(detection)}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate font-semibold text-text" title={detection.rule_name ?? undefined}>
                  {detection.rule_name || detection.dedup_key}
                </div>
                <div className="mt-0.5 text-xs text-text-secondary">{t(sourceLabelKey(detection.source))}</div>
              </div>
              <DetectionSeverityBadge severityId={detection.severity_id} />
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Badge variant={statusBadgeVariant(detection.status)} size="sm">
                {t(statusLabelKey(detection.status))}
              </Badge>
              <span className="font-mono text-xs tabular-nums text-text-tertiary">
                {t("schedules:detections.table.occurrenceCount", { count: detection.count ?? 1 })}
              </span>
              {detection.last_seen && (
                <span className="text-xs text-text-tertiary">
                  {t("schedules:detections.table.lastSeenLabel", { date: formatDate(detection.last_seen) })}
                </span>
              )}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}

export default DetectionsTable
