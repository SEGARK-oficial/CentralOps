/**
 * severity.ts - encoding multi-canal colorblind-safe (Fase 4 / C5).
 *
 * Nunca retorna cor sozinha. Cada nivel carrega:
 *   colorToken   - classe Tailwind do design system (text-* / bg-*)
 *   iconName     - nome Lucide (para lookup ou import direto)
 *   label        - rotulo PT-BR exibivel ao usuario
 *   badgeVariant - variante do componente Badge
 *
 * Cobre tres dominios:
 *   - HealthStatus   : healthy / degraded / down / unknown
 *   - AlertSeverity  : ok / warn / error / critical
 *   - PipelineStatus : route / drop / quarantine / unknown
 */

import type React from "react"
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  MinusCircle,
  Activity,
  AlertCircle,
  TrendingDown,
  Minus,
  type LucideIcon,
} from "lucide-react"

// -- Tipos base ---------------------------------------------------------------

export type BadgeVariant = "success" | "warning" | "danger" | "primary" | "outline" | "default"

/** Encoding completo para um nivel de severidade/status. */
export interface SeverityEncoding {
  /** Classe Tailwind para texto (usa token do DS). */
  colorToken: string
  /** Classe Tailwind para background suave (usa token do DS). */
  bgToken: string
  /** Icone Lucide - canal visual independente de cor. */
  Icon: LucideIcon
  /** Nome do icone (para serializacao/lookup). */
  iconName: string
  /** Rotulo PT-BR exibivel. */
  label: string
  /** Variante do Badge do DS. */
  badgeVariant: BadgeVariant
}

// -- HealthStatus -------------------------------------------------------------

export type HealthStatus = "healthy" | "degraded" | "down" | "unknown"

const HEALTH_MAP: Record<HealthStatus, SeverityEncoding> = {
  healthy: {
    colorToken:   "text-success-700",
    bgToken:      "bg-success-50",
    Icon:         CheckCircle2,
    iconName:     "check-circle-2",
    label:        "Saudável",
    badgeVariant: "success",
  },
  degraded: {
    colorToken:   "text-warning-700",
    bgToken:      "bg-warning-50",
    Icon:         AlertTriangle,
    iconName:     "alert-triangle",
    label:        "Degradado",
    badgeVariant: "warning",
  },
  down: {
    colorToken:   "text-danger-700",
    bgToken:      "bg-danger-50",
    Icon:         XCircle,
    iconName:     "x-circle",
    label:        "Indisponível",
    badgeVariant: "danger",
  },
  unknown: {
    colorToken:   "text-text-tertiary",
    bgToken:      "bg-surface-tertiary",
    Icon:         MinusCircle,
    iconName:     "minus-circle",
    label:        "Desconhecido",
    badgeVariant: "outline",
  },
}

export function healthEncoding(status?: string | null): SeverityEncoding {
  const key = (status ?? "").toLowerCase() as HealthStatus
  return HEALTH_MAP[key] ?? HEALTH_MAP.unknown
}

// -- AlertSeverity ------------------------------------------------------------

export type AlertSeverity = "ok" | "warn" | "error" | "critical"

const ALERT_MAP: Record<AlertSeverity, SeverityEncoding> = {
  ok: {
    colorToken:   "text-success-700",
    bgToken:      "bg-success-50",
    Icon:         CheckCircle2,
    iconName:     "check-circle-2",
    label:        "OK",
    badgeVariant: "success",
  },
  warn: {
    colorToken:   "text-warning-700",
    bgToken:      "bg-warning-50",
    Icon:         AlertTriangle,
    iconName:     "alert-triangle",
    label:        "Atenção",
    badgeVariant: "warning",
  },
  error: {
    colorToken:   "text-danger-700",
    bgToken:      "bg-danger-50",
    Icon:         AlertCircle,
    iconName:     "alert-circle",
    label:        "Erro",
    badgeVariant: "danger",
  },
  critical: {
    colorToken:   "text-danger-700",
    bgToken:      "bg-danger-100",
    Icon:         XCircle,
    iconName:     "x-circle",
    label:        "Crítico",
    badgeVariant: "danger",
  },
}

export function alertEncoding(severity?: string | null): SeverityEncoding {
  const key = (severity ?? "").toLowerCase() as AlertSeverity
  return ALERT_MAP[key] ?? ALERT_MAP.error
}

// -- PipelineStatus -----------------------------------------------------------

export type PipelineStatus = "route" | "drop" | "quarantine" | "unknown"

const PIPELINE_MAP: Record<PipelineStatus, SeverityEncoding> = {
  route: {
    colorToken:   "text-primary-700",
    bgToken:      "bg-primary-100",
    Icon:         Activity,
    iconName:     "activity",
    label:        "Roteado",
    badgeVariant: "primary",
  },
  drop: {
    colorToken:   "text-danger-700",
    bgToken:      "bg-danger-50",
    Icon:         TrendingDown,
    iconName:     "trending-down",
    label:        "Descartado",
    badgeVariant: "danger",
  },
  quarantine: {
    colorToken:   "text-warning-700",
    bgToken:      "bg-warning-50",
    Icon:         AlertTriangle,
    iconName:     "alert-triangle",
    label:        "Quarentena",
    badgeVariant: "warning",
  },
  unknown: {
    colorToken:   "text-text-tertiary",
    bgToken:      "bg-surface-tertiary",
    Icon:         Minus,
    iconName:     "minus",
    label:        "Desconhecido",
    badgeVariant: "outline",
  },
}

export function pipelineEncoding(status?: string | null): SeverityEncoding {
  const key = (status ?? "").toLowerCase() as PipelineStatus
  return PIPELINE_MAP[key] ?? PIPELINE_MAP.unknown
}

// -- Mapas exportados (lookup direto) -----------------------------------------

export { HEALTH_MAP, ALERT_MAP, PIPELINE_MAP }

// -- StatusBadge - componente leve reutilizavel -------------------------------

export interface StatusBadgeProps {
  encoding: SeverityEncoding
  /** Tamanho do icone em px (default 14). */
  iconSize?: number
  /** Inclui texto label ao lado do icone (default true). */
  showLabel?: boolean
  className?: string
}

/**
 * StatusBadge: renderiza icone Lucide + label + cor via tokens do DS.
 * Colorblind-safe: nunca depende so de cor para transmitir informacao.
 *
 * Uso:
 *   const enc = healthEncoding("degraded")
 *   <StatusBadge encoding={enc} />
 */
export function StatusBadge({
  encoding,
  iconSize = 14,
  showLabel = true,
  className = "",
}: StatusBadgeProps): React.ReactElement {
  const { Icon, label, colorToken, bgToken } = encoding
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${bgToken} ${colorToken} ${className}`}
      aria-label={label}
    >
      <Icon size={iconSize} aria-hidden="true" />
      {showLabel && <span>{label}</span>}
    </span>
  ) as React.ReactElement
}
