"use client"

import type React from "react"
import { useId, useMemo } from "react"
import { useTranslation } from "react-i18next"
import { ShieldAlertIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import { detectionSeverityEncoding } from "@/lib/severity"
import type { DetectionRead, DetectionSource, DetectionStatus, TableColumn } from "@/types"
import { formatDate } from "@/lib/utils"

interface DetectionsTableProps {
  detections: DetectionRead[]
  loading?: boolean
  onRowClick: (detection: DetectionRead) => void
}

// R2-5.3: acima disto a lista virtualiza via DataTable (useDetections pede
// até 200 linhas por vez). Abaixo, tudo no DOM.
const VIRTUALIZE_THRESHOLD = 60
const MAX_HEIGHT_PX = 560

// ── Helpers ──────────────────────────────────────────────────────────────────

type TFn = ReturnType<typeof useTranslation>["t"]

function sourceLabelKey(source: DetectionSource): string {
  switch (source) {
    case "scheduled_query":
      return "detections:list.source.scheduled_query"
    case "live_query":
      return "detections:list.source.live_query"
    case "correlation":
      return "detections:list.source.correlation"
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
      return "detections:list.statusSingular.open"
    case "ack":
      return "detections:list.statusSingular.ack"
    case "closed":
      return "detections:list.statusSingular.closed"
    default:
      return status
  }
}

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
    // A11Y-14: a linha é um <tr> comum do DataTable (a semântica de linha de
    // tabela não é apagada por um role de botão que não existe em HTML), e só
    // a célula da regra — que é o alvo natural do clique — vira <button>.
    // `focus-ring`: mesma estratégia de foco do design system em toda a app.
    <button
      type="button"
      className="max-w-[280px] space-y-0.5 rounded text-left focus-ring"
      aria-label={t("detections:list.table.rowAriaLabel", { name: detection.rule_name || detection.dedup_key })}
      onClick={() => onRowClick(detection)}
    >
      <div className="truncate font-medium text-text" title={detection.rule_name ?? undefined}>
        {detection.rule_name || "-"}
      </div>
      {detection.rule_id && <div className="font-mono text-xs text-text-tertiary">{detection.rule_id}</div>}
    </button>
  )
}

/**
 * Cartão mobile — R2-5.3 (item BAIXA): `aria-describedby` em vez de um
 * `aria-label` só, que amontoava fonte+status+contagem+data numa frase só
 * traduzida. Nome acessível vem do TÍTULO de verdade (`aria-labelledby`); o
 * resto (fonte, severidade, status, contagem, última vez) é DESCRIÇÃO
 * (`aria-describedby`, múltiplos ids) — a mesma informação visível, sem
 * reconstruir uma frase à parte pro leitor de tela.
 */
function DetectionMobileCard({ detection, t, onRowClick }: { detection: DetectionRead; t: TFn; onRowClick: (d: DetectionRead) => void }) {
  const titleId = useId()
  const sourceId = useId()
  const severityId = useId()
  const metaId = useId()
  return (
    <button
      type="button"
      className="w-full rounded-xl border border-border bg-surface p-4 text-left transition-colors hover:bg-surface-tertiary/40 focus-ring"
      aria-labelledby={titleId}
      aria-describedby={`${sourceId} ${severityId} ${metaId}`}
      onClick={() => onRowClick(detection)}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div id={titleId} className="truncate font-semibold text-text" title={detection.rule_name ?? undefined}>
            {detection.rule_name || detection.dedup_key}
          </div>
          <div id={sourceId} className="mt-0.5 text-xs text-text-secondary">
            {t(sourceLabelKey(detection.source))}
          </div>
        </div>
        <span id={severityId}>
          <DetectionSeverityBadge severityId={detection.severity_id} />
        </span>
      </div>
      <div id={metaId} className="mt-3 flex flex-wrap items-center gap-2">
        <Badge variant={statusBadgeVariant(detection.status)} size="sm">
          {t(statusLabelKey(detection.status))}
        </Badge>
        <span className="font-mono text-xs tabular-nums text-text-tertiary">
          {t("detections:list.table.occurrenceCount", { count: detection.count ?? 1 })}
        </span>
        {detection.last_seen && (
          <span className="text-xs text-text-tertiary">
            {t("detections:list.table.lastSeenLabel", { date: formatDate(detection.last_seen) })}
          </span>
        )}
      </div>
    </button>
  )
}

// ── Component ─────────────────────────────────────────────────────────────────

export const DetectionsTable: React.FC<DetectionsTableProps> = ({ detections, loading = false, onRowClick }) => {
  const { t } = useTranslation("detections")

  const columns: TableColumn<DetectionRead>[] = useMemo(
    () => [
      {
        key: "severity",
        title: t("detections:list.table.columns.severity"),
        dataIndex: "severity_id",
        className: "whitespace-nowrap",
        render: (_v, d) => <DetectionSeverityBadge severityId={d.severity_id} />,
      },
      {
        key: "source",
        title: t("detections:list.table.columns.source"),
        dataIndex: "source",
        render: (_v, d) => <span className="text-text-secondary">{t(sourceLabelKey(d.source))}</span>,
      },
      {
        key: "rule",
        title: t("detections:list.table.columns.rule"),
        dataIndex: "rule_name",
        render: (_v, d) => <DetectionRuleButton detection={d} t={t} onRowClick={onRowClick} />,
      },
      {
        key: "status",
        title: t("detections:list.table.columns.status"),
        dataIndex: "status",
        className: "whitespace-nowrap",
        render: (_v, d) => (
          <Badge variant={statusBadgeVariant(d.status)} size="sm">
            {t(statusLabelKey(d.status))}
          </Badge>
        ),
      },
      {
        key: "occurrences",
        title: t("detections:list.table.columns.occurrences"),
        dataIndex: "count",
        align: "right",
        render: (_v, d) => d.count ?? 1,
      },
      {
        key: "lastSeen",
        title: t("detections:list.table.columns.lastSeen"),
        dataIndex: "last_seen",
        className: "whitespace-nowrap",
        render: (_v, d) => (d.last_seen ? formatDate(d.last_seen) : "-"),
      },
    ],
    [t, onRowClick],
  )

  if (!loading && detections.length === 0) {
    return (
      <EmptyState
        icon={<ShieldAlertIcon size={48} />}
        title={t("detections:list.table.emptyTitle")}
        description={t("detections:list.table.emptyDescription")}
      />
    )
  }

  return (
    <DataTable<DetectionRead>
      data={detections}
      columns={columns}
      loading={loading}
      rowKey="id"
      virtualizeRows={detections.length > VIRTUALIZE_THRESHOLD}
      maxHeight={MAX_HEIGHT_PX}
      tableClassName="min-w-[860px]"
      tableAriaLabel={t("detections:list.table.ariaLabel")}
      emptyMessage={t("detections:list.table.emptyTitle")}
      renderMobileCard={(d) => <DetectionMobileCard detection={d} t={t} onRowClick={onRowClick} />}
    />
  )
}

export default DetectionsTable
