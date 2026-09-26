/**
 * severity.tsx - encoding multi-canal colorblind-safe (Fase 4 / C5).
 *
 * Nunca retorna cor sozinha. Cada nivel carrega:
 *   colorToken   - classe Tailwind do design system (text-* / bg-*)
 *   iconName     - nome Lucide (para lookup ou import direto)
 *   labelKey     - chave i18n (namespace `dashboard`), resolvida na renderizacao
 *   badgeVariant - variante do componente Badge
 *
 * Cobre um dominio:
 *   - HealthStatus : healthy / degraded / down / disabled / unknown
 *
 * Ja houve aqui um ALERT_MAP e um PIPELINE_MAP. Os dois morreram sem call site
 * e o PIPELINE_MAP guardava o encoding ANTIGO (route=violeta, drop=vermelhao),
 * o oposto do que o FlowCanvas pinta hoje (route=ciano, drop=teal). Encoding
 * contraditorio dormindo no repo e armadilha: quem viesse depois importaria o
 * mapa e reintroduziria a divergencia entre /routes e /flow. Removidos.
 *
 * O rotulo e chave, nao texto: o mapa e um modulo sem contexto de React, e texto
 * fixo aqui saia em portugues com o resto da UI em ingles. Mesmo padrao do
 * BREAKER_LABEL_KEY em DestinationHealthGrid.
 */

import type React from "react"
import { useTranslation } from "react-i18next"
import {
  CheckCircle2,
  AlertTriangle,
  XCircle,
  MinusCircle,
  PowerOff,
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
  /** Chave i18n do rotulo, namespace `dashboard`. */
  labelKey: string
  /** Variante do Badge do DS. */
  badgeVariant: BadgeVariant
}

// -- HealthStatus -------------------------------------------------------------

export type HealthStatus = "healthy" | "degraded" | "down" | "disabled" | "unknown"

// Saudavel e NEUTRO, igual ao HealthBadge e ao FlowCanvas. O operador varre a
// tela procurando o que NAO esta neutro; teal em "continua ok" enche a grade de
// matiz e afoga o degradado. O icone segue distinguindo os niveis sem cor.
//
// As chaves reusam `health.badge.*` de proposito: /destinations e /pipeline-health
// leem do mesmo catalogo, entao nao ha como uma tela dizer "Indisponivel" e a
// outra "Down" para o mesmo estado.
const HEALTH_MAP: Record<HealthStatus, SeverityEncoding> = {
  healthy: {
    colorToken:   "text-text-secondary",
    bgToken:      "bg-surface-tertiary",
    Icon:         CheckCircle2,
    iconName:     "check-circle-2",
    labelKey:     "health.badge.healthy",
    badgeVariant: "default",
  },
  degraded: {
    colorToken:   "text-warning-700",
    bgToken:      "bg-warning-50",
    Icon:         AlertTriangle,
    iconName:     "alert-triangle",
    labelKey:     "health.badge.degraded",
    badgeVariant: "warning",
  },
  down: {
    colorToken:   "text-danger-700",
    bgToken:      "bg-danger-50",
    Icon:         XCircle,
    iconName:     "x-circle",
    labelKey:     "health.badge.unhealthy",
    badgeVariant: "danger",
  },
  // Destino desligado e escolha do operador, nao incidente. Colapsar `disabled`
  // em `down` acendia vermelhao e dizia "Indisponivel" para quem tinha acabado
  // de desligar o destino de proposito: alarme falso em toda varredura da grade.
  // Estado proprio, neutro, com icone que ja diz o que houve.
  disabled: {
    colorToken:   "text-text-tertiary",
    bgToken:      "bg-surface-tertiary",
    Icon:         PowerOff,
    iconName:     "power-off",
    labelKey:     "health.badge.disabled",
    badgeVariant: "outline",
  },
  unknown: {
    colorToken:   "text-text-tertiary",
    bgToken:      "bg-surface-tertiary",
    Icon:         MinusCircle,
    iconName:     "minus-circle",
    labelKey:     "health.badge.unknown",
    badgeVariant: "outline",
  },
}

// -- DetectionSeverity (OCSF 1-6) ---------------------------------------------

/**
 * Encoding de severidade de uma Detection (OCSF 1=Informational … 6=Fatal).
 *
 * Domínio à parte do HealthStatus acima (severidade de EVENTO, não saúde de
 * destino), mas a mesma regra colorblind-safe: variant + labelKey, nunca cor
 * sozinha. DetectionsTable usava `variant="primary"` para a severidade 3
 * (Média) — primary é VIOLETA, a cor semântica de normalize/OCSF/marca no
 * design system, não um nível de risco. Aqui ela cai para neutro, como
 * Informational/Low; só High (4) e Critical/Fatal (5-6) carregam matiz de
 * alerta (warning/danger). Helper único para não deixar cada tela reinventar
 * o mapeamento — DetectionDetailsDrawer (Sub 8) também consome daqui.
 */
const DETECTION_SEVERITY_LABEL_KEYS: Record<number, string> = {
  1: "schedules:detections.severity.informational",
  2: "schedules:detections.severity.low",
  3: "schedules:detections.severity.medium",
  4: "schedules:detections.severity.high",
  5: "schedules:detections.severity.critical",
  6: "schedules:detections.severity.fatal",
}

export interface DetectionSeverityEncoding {
  badgeVariant: BadgeVariant
  labelKey: string
  /** Interpolação para o labelKey de fallback (severidade fora de 1-6). */
  labelParams?: Record<string, unknown>
}

export function detectionSeverityEncoding(severityId: number): DetectionSeverityEncoding {
  const badgeVariant: BadgeVariant = severityId <= 3 ? "default" : severityId === 4 ? "warning" : "danger"
  const labelKey = DETECTION_SEVERITY_LABEL_KEYS[severityId]
  if (labelKey) return { badgeVariant, labelKey }
  return { badgeVariant, labelKey: "schedules:detections.severity.unknown", labelParams: { id: severityId } }
}

/**
 * O contrato de saúde de destino (`DestinationHealthStatus`) diz "unhealthy"
 * onde este mapa diz "down". Sem o alias o valor caía no fallback `unknown`: um
 * destino fora do ar aparecia como "Aguardando coleta" cinza em /destinations e
 * como "Indisponível" vermelho em /pipeline-health. O mesmo destino, dois
 * cliques de distância.
 */
const HEALTH_ALIAS: Record<string, HealthStatus> = {
  unhealthy: "down",
}

export function healthEncoding(status?: string | null): SeverityEncoding {
  const raw = (status ?? "").toLowerCase()
  const key = (HEALTH_ALIAS[raw] ?? raw) as HealthStatus
  return HEALTH_MAP[key] ?? HEALTH_MAP.unknown
}

// -- Mapas exportados (lookup direto) -----------------------------------------

export { HEALTH_MAP }

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
  const { t } = useTranslation("dashboard")
  const { Icon, labelKey, colorToken, bgToken } = encoding
  const label = t(labelKey)
  // A11Y-34: `aria-label` num `<span>` sem `role` não é exposto de forma
  // confiável ao leitor de tela (elemento genérico, fora da árvore de
  // acessibilidade por padrão). O nome acessível vem do texto de verdade:
  // visível quando `showLabel`, ou `sr-only` no modo ícone-apenas — os dois
  // sempre entram na árvore, ao contrário do atributo.
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${bgToken} ${colorToken} ${className}`}
    >
      <Icon size={iconSize} aria-hidden="true" />
      {showLabel ? <span>{label}</span> : <span className="sr-only">{label}</span>}
    </span>
  ) as React.ReactElement
}
