"use client"

import type React from "react"
import { ShieldAlertIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { EmptyState } from "@/components/ui/EmptyState/EmptyState"
import LoadingSpinner from "@/components/ui/LoadingSpinner/LoadingSpinner"
import type { DetectionRead, DetectionSource, DetectionStatus } from "@/types"
import { formatDate } from "@/lib/utils"

interface DetectionsTableProps {
  detections: DetectionRead[]
  loading?: boolean
  onRowClick: (detection: DetectionRead) => void
}

// ── Helpers ──────────────────────────────────────────────────────────────────

type BadgeVariant = "default" | "primary" | "success" | "warning" | "danger" | "outline"

function severityBadgeVariant(severityId: number): BadgeVariant {
  if (severityId <= 2) return "default"   // Informational / Low (OCSF 1-2)
  if (severityId === 3) return "primary"  // Medium (OCSF 3 = baixa/low)
  if (severityId === 4) return "warning"  // High (OCSF 4)
  return "danger"                          // Critical / Fatal (OCSF 5-6)
}

function severityLabel(severityId: number): string {
  switch (severityId) {
    case 1: return "Informacional"
    case 2: return "Baixa"
    case 3: return "Média"
    case 4: return "Alta"
    case 5: return "Crítica"
    case 6: return "Fatal"
    default: return `Sev ${severityId}`
  }
}

function sourceLabel(source: DetectionSource): string {
  switch (source) {
    case "scheduled_query": return "Query agendada"
    case "live_query": return "Query live"
    case "correlation": return "Correlação"
    default: return source
  }
}

function statusBadgeVariant(status: DetectionStatus): BadgeVariant {
  switch (status) {
    case "open": return "danger"
    case "ack": return "warning"
    case "closed": return "success"
    default: return "default"
  }
}

function statusLabel(status: DetectionStatus): string {
  switch (status) {
    case "open": return "Aberta"
    case "ack": return "Reconhecida"
    case "closed": return "Fechada"
    default: return status
  }
}

// ── Classes ───────────────────────────────────────────────────────────────────

const thCls = "px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-text-secondary"
const tdCls = "px-4 py-3 text-sm align-top"

// ── Component ─────────────────────────────────────────────────────────────────

export const DetectionsTable: React.FC<DetectionsTableProps> = ({
  detections,
  loading = false,
  onRowClick,
}) => {
  if (loading) {
    return (
      <div className="flex min-h-[240px] items-center justify-center">
        <LoadingSpinner size="lg" text="Carregando detecções..." />
      </div>
    )
  }

  if (detections.length === 0) {
    return (
      <EmptyState
        icon={<ShieldAlertIcon size={48} />}
        title="Nenhuma detecção encontrada"
        description="Ajuste o filtro de status ou aguarde novas detecções das queries agendadas e regras de correlação."
      />
    )
  }

  return (
    <div className="space-y-4">
      {/* Desktop: tabela com rolagem horizontal segura */}
      <div className="hidden overflow-hidden rounded-xl border border-border md:block">
        <div className="overflow-x-auto">
          <table
            className="w-full min-w-[860px] text-sm"
            role="table"
            aria-label="Lista de detecções"
          >
            <thead className="bg-surface-tertiary">
              <tr className="border-b border-border">
                <th scope="col" className={`${thCls} whitespace-nowrap`}>Severidade</th>
                <th scope="col" className={thCls}>Fonte</th>
                <th scope="col" className={thCls}>Regra</th>
                <th scope="col" className={`${thCls} whitespace-nowrap`}>Status</th>
                <th scope="col" className={`${thCls} text-right whitespace-nowrap`}>Ocorrências</th>
                <th scope="col" className={`${thCls} whitespace-nowrap`}>Última vez vista</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border bg-surface">
              {detections.map((detection) => (
                <tr
                  key={detection.id}
                  className="cursor-pointer transition-colors hover:bg-surface-tertiary/40 focus-visible:bg-surface-tertiary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary-500/40"
                  role="button"
                  tabIndex={0}
                  aria-label={`Ver detalhes da detecção ${detection.rule_name || detection.dedup_key}`}
                  onClick={() => onRowClick(detection)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault()
                      onRowClick(detection)
                    }
                  }}
                >
                  <td className={tdCls}>
                    <Badge variant={severityBadgeVariant(detection.severity_id)} size="sm">
                      {severityLabel(detection.severity_id)}
                    </Badge>
                  </td>
                  <td className={tdCls}>
                    <span className="text-text-secondary">{sourceLabel(detection.source)}</span>
                  </td>
                  <td className={tdCls}>
                    <div className="max-w-[280px] space-y-0.5">
                      <div className="truncate font-medium text-text" title={detection.rule_name ?? undefined}>
                        {detection.rule_name || "-"}
                      </div>
                      {detection.rule_id && (
                        <div className="font-mono text-xs text-text-tertiary">{detection.rule_id}</div>
                      )}
                    </div>
                  </td>
                  <td className={tdCls}>
                    <Badge variant={statusBadgeVariant(detection.status)} size="sm">
                      {statusLabel(detection.status)}
                    </Badge>
                  </td>
                  <td className={`${tdCls} text-right font-semibold text-text`}>
                    {detection.count ?? 1}
                  </td>
                  <td className={`${tdCls} whitespace-nowrap text-xs text-text-secondary`}>
                    {detection.last_seen ? formatDate(detection.last_seen) : "-"}
                  </td>
                </tr>
              ))}
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
            className="w-full rounded-xl border border-border bg-surface p-4 text-left transition-colors hover:bg-surface-tertiary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500/40"
            aria-label={`Ver detalhes da detecção ${detection.rule_name || detection.dedup_key}`}
            onClick={() => onRowClick(detection)}
          >
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate font-semibold text-text" title={detection.rule_name ?? undefined}>
                  {detection.rule_name || detection.dedup_key}
                </div>
                <div className="mt-0.5 text-xs text-text-secondary">{sourceLabel(detection.source)}</div>
              </div>
              <Badge variant={severityBadgeVariant(detection.severity_id)} size="sm">
                {severityLabel(detection.severity_id)}
              </Badge>
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Badge variant={statusBadgeVariant(detection.status)} size="sm">
                {statusLabel(detection.status)}
              </Badge>
              <span className="text-xs text-text-tertiary">
                {detection.count ?? 1} ocorrência(s)
              </span>
              {detection.last_seen && (
                <span className="text-xs text-text-tertiary">
                  Última: {formatDate(detection.last_seen)}
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
