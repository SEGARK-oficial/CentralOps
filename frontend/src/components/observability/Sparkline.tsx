import type React from "react"
import { useTranslation } from "react-i18next"
import { formatDateTime } from "@/lib/intl"

// Mapa de variante para token de cor do design system
const VARIANT_COLOR: Record<SparklineVariant, string> = {
  neutral: "#94a3b8",   // text-tertiary
  primary: "#0ea5e9",   // primary-500
  success: "#22c55e",   // success-500
  danger:  "#ef4444",   // danger-500
  warning: "#f59e0b",   // warning-500
}


export type SparklineVariant = "neutral" | "primary" | "success" | "danger" | "warning"

export interface SparklineProps {
  /** Pares [timestamp_ms, valor] */
  points: [number, number | string][]
  /** Label legível — usado em aria-label e na legenda */
  label: string
  variant?: SparklineVariant
  width?: number
  height?: number
  /** Exibe linha de baseline (zero) */
  showBaseline?: boolean
  /** Exibe marcadores min/max */
  showMinMax?: boolean
}

/** Formata timestamp unix-ms para exibição no tooltip (locale ativo). */
function fmtTime(ts: number): string {
  return formatDateTime(ts, { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}

/**
 * Sparkline SVG dependency-free com:
 * - Variante colorblind-safe via prop (não apenas cor).
 * - Area fill sutil com gradiente.
 * - Tooltip nativo (<title>) no ponto ativo.
 * - Marcador de 1 ponto (não retorna "sem dados" prematuramente).
 * - Acessível: role="img" + aria-label com resumo.
 */
export const Sparkline: React.FC<SparklineProps> = ({
  points,
  label,
  variant = "neutral",
  width = 160,
  height = 32,
  showBaseline = false,
  showMinMax = false,
}) => {
  const { t } = useTranslation("dashboard")
  const vals = points.map((p) => Number(p[1])).filter((n) => !Number.isNaN(n))
  const color = VARIANT_COLOR[variant]

  // 0 pontos: sem dados
  if (vals.length === 0) {
    return <span className="text-xs text-text-tertiary">{t("observability.sparkline.noData", { label })}</span>
  }

  const last    = vals[vals.length - 1]
  const lastTs  = points[points.length - 1]?.[0]
  const lastValuePart = lastTs
    ? t("observability.sparkline.lastValueAt", { label, value: last.toFixed(2), time: fmtTime(lastTs) })
    : t("observability.sparkline.lastValue", { label, value: last.toFixed(2) })
  const ariaSum = `${lastValuePart}, ${t("observability.sparkline.pointsCount", { count: vals.length })}`

  // 1 ponto: exibe marcador central + valor, sem linha
  if (vals.length === 1) {
    const cx = width / 2
    const cy = height / 2
    const gradId = `sp-single-${variant}`
    return (
      <div className="flex items-center gap-2">
        <svg width={width} height={height} role="img" aria-label={ariaSum}>
          <title>{lastValuePart}</title>
          <defs>
            <radialGradient id={gradId} cx="50%" cy="50%" r="50%">
              <stop offset="0%" stopColor={color} stopOpacity={0.25} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </radialGradient>
          </defs>
          <ellipse cx={cx} cy={cy} rx={18} ry={12} fill={`url(#${gradId})`} />
          <circle cx={cx} cy={cy} r={4} fill={color} />
          <circle cx={cx} cy={cy} r={6} fill="none" stroke={color} strokeWidth={1.5} strokeOpacity={0.4} />
        </svg>
        <div className="text-xs">
          <div className="font-medium text-text">{last.toFixed(2)}</div>
          <div className="text-text-tertiary">{label}</div>
        </div>
      </div>
    )
  }

  // N>=2 pontos: sparkline completa
  const max  = Math.max(...vals, 0.0001)
  const min  = Math.min(...vals, 0)
  const span = max - min || 1

  // padding vertical para que o traço não seja cortado na borda
  const pad = 3
  const plotH = height - pad * 2

  const toXY = (v: number, i: number): [number, number] => {
    const x = (i / (vals.length - 1)) * width
    const y = pad + plotH - ((v - min) / span) * plotH
    return [x, y]
  }

  const coords = vals.map((v, i) => toXY(v, i))
  const linePath = coords.map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ")

  // Area fill: fecha no baseline
  const [x0] = coords[0]
  const [xN] = coords[coords.length - 1]
  const baseline  = pad + plotH  // y da borda inferior
  const areaPath  = `${linePath} L${xN.toFixed(1)},${baseline} L${x0.toFixed(1)},${baseline} Z`

  // Último ponto para o marcador
  const [lx, ly] = coords[coords.length - 1]

  // Índices min e max (para labels opcionais)
  const minIdx = vals.indexOf(Math.min(...vals))
  const maxIdx = vals.indexOf(Math.max(...vals))

  const gradId = `sp-area-${variant}-${Math.random().toString(36).slice(2, 6)}`

  return (
    <div className="flex items-center gap-2">
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={ariaSum}
        style={{ overflow: "visible" }}
      >
        <title>{ariaSum}</title>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"   stopColor={color} stopOpacity={0.35} />
            <stop offset="100%" stopColor={color} stopOpacity={0.02} />
          </linearGradient>
        </defs>

        {/* Baseline */}
        {showBaseline && (
          <line
            x1={0}
            y1={baseline}
            x2={width}
            y2={baseline}
            stroke={color}
            strokeWidth={0.5}
            strokeOpacity={0.3}
            strokeDasharray="2 3"
          />
        )}

        {/* Area fill */}
        <path d={areaPath} fill={`url(#${gradId})`} />

        {/* Linha principal */}
        <path d={linePath} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />

        {/* Marcadores min/max opcionais */}
        {showMinMax && minIdx !== maxIdx && (
          <>
            <circle cx={coords[minIdx][0].toFixed(1)} cy={coords[minIdx][1].toFixed(1)} r={3} fill={color} fillOpacity={0.5}>
              <title>{t("observability.sparkline.min", { value: vals[minIdx].toFixed(2) })}</title>
            </circle>
            <circle cx={coords[maxIdx][0].toFixed(1)} cy={coords[maxIdx][1].toFixed(1)} r={3} fill={color}>
              <title>{t("observability.sparkline.max", { value: vals[maxIdx].toFixed(2) })}</title>
            </circle>
          </>
        )}

        {/* Marcador do último ponto + tooltip */}
        <circle cx={lx.toFixed(1)} cy={ly.toFixed(1)} r={3} fill={color}>
          <title>{lastValuePart}</title>
        </circle>
        <circle cx={lx.toFixed(1)} cy={ly.toFixed(1)} r={5} fill="none" stroke={color} strokeWidth={1} strokeOpacity={0.4} />
      </svg>

      <div className="text-xs">
        <div className="font-medium text-text">{last.toFixed(2)}</div>
        <div className="text-text-tertiary">{label}</div>
      </div>
    </div>
  )
}
