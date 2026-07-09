import type React from "react"
import { ArrowDownIcon, ArrowRightIcon, ArrowUpIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Card } from "@/components/ui/Card/Card"
import { iconFor } from "@/lib/icons"
import { cn } from "@/lib/utils"
import type { DashSeverity, KpiCard } from "@/types"

// ── Severity → Badge variant ─────────────────────────────────────────────────

const SEVERITY_VARIANT: Record<DashSeverity, "success" | "warning" | "danger" | "primary"> = {
  ok: "success",
  warn: "warning",
  critical: "danger",
  info: "primary",
}

// Border-left accent only for critical
const CARD_ACCENT: Record<DashSeverity, string> = {
  ok: "",
  warn: "",
  critical: "border-l-4 border-l-danger-500",
  info: "",
}

// ── Trend chip ────────────────────────────────────────────────────────────────

interface TrendChipProps {
  trend: "up" | "down" | "flat"
  trendValue?: string | null
}

const TrendChip: React.FC<TrendChipProps> = ({ trend, trendValue }) => {
  const Icon = trend === "up" ? ArrowUpIcon : trend === "down" ? ArrowDownIcon : ArrowRightIcon
  const colorCls =
    trend === "up"
      ? "text-danger-600"
      : trend === "down"
        ? "text-success-600"
        : "text-text-tertiary"

  return (
    <span
      className={cn("inline-flex items-center gap-0.5 text-xs font-medium", colorCls)}
      aria-label={`Tendência: ${trend}${trendValue ? ` ${trendValue}` : ""}`}
    >
      <Icon size={12} aria-hidden="true" />
      {trendValue && <span className="hidden sm:inline">{trendValue}</span>}
    </span>
  )
}

// ── Single KPI card ───────────────────────────────────────────────────────────

interface KpiCardItemProps {
  kpi: KpiCard
}

const KpiCardItem: React.FC<KpiCardItemProps> = ({ kpi }) => {
  const Icon = iconFor(kpi.icon_id)
  const accentCls = kpi.severity ? (CARD_ACCENT[kpi.severity] ?? "") : ""

  return (
    <Card
      padding="sm"
      className={cn("shadow-sm flex flex-col gap-2", accentCls)}
      aria-label={`KPI: ${kpi.label}, valor: ${kpi.value}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-text-tertiary line-clamp-1">
          {kpi.label}
        </span>
        <Icon size={16} className="shrink-0 text-text-tertiary" aria-hidden="true" />
      </div>

      <div className="text-2xl font-bold text-text leading-none">{kpi.value}</div>

      <div className="flex items-center justify-between gap-2">
        {kpi.sub && (
          <span className="text-xs text-text-secondary line-clamp-1">{kpi.sub}</span>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          {kpi.trend && kpi.trend !== "flat" && (
            <TrendChip trend={kpi.trend} trendValue={kpi.trend_value} />
          )}
          {kpi.severity && (
            <Badge variant={SEVERITY_VARIANT[kpi.severity]} size="sm">
              {kpi.severity}
            </Badge>
          )}
        </div>
      </div>
    </Card>
  )
}

// ── KpiGrid ───────────────────────────────────────────────────────────────────

interface KpiGridProps {
  kpis: KpiCard[]
}

export const KpiGrid: React.FC<KpiGridProps> = ({ kpis }) => {
  if (kpis.length === 0) return null

  return (
    <div
      className="grid gap-4 grid-cols-2 md:grid-cols-3 xl:grid-cols-6"
      role="region"
      aria-label="KPIs do dashboard"
    >
      {kpis.map((kpi) => (
        <KpiCardItem key={kpi.id} kpi={kpi} />
      ))}
    </div>
  )
}
