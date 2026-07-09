/**
 * RoutingTopology — grafo SVG leve (sem lib externa) do fluxo de roteamento.
 *
 * Layout: Coleta (source) → Rotas (nodes) → Destinos (sink nodes).
 * Arestas reais via <path> com curvas bezier.
 * Cor dos nós usa tokens do design system via healthEncoding/pipelineEncoding.
 * Destino marcado como "drop" recebe cor danger; outros success/outline.
 * Aresta "fan-out" (is_final=false) marcada com tracejado.
 * Acessível: role="img" + aria-label descritivo como texto alternativo.
 *
 * flow-view com throughput:
 * quando a prop OPCIONAL `topology` é fornecida, as arestas rota→destino
 * ganham espessura ∝ routed_per_min, ganham rótulos de eventos/min, os nós
 * de destino são coloridos por `status` e mostram EPS. Sem a prop, o
 * componente renderiza EXATAMENTE como antes (backward-compatible).
 */

import type React from "react"
import { useMemo } from "react"
import { useTranslation } from "react-i18next"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { fmtRate } from "@/lib/fmt"
import type { Route, RoutingTopologyResponse, DestinationHealthStatus } from "@/types"

// ── Layout constants ──────────────────────────────────────────────────────

const COL_X = { source: 60, route: 220, dest: 420 }
const NODE_W = 130
const NODE_H = 36
const ROW_GAP = 12
const SVG_PAD_Y = 24

// Largura da aresta rota→destino quando há throughput (clamp sensato).
const EDGE_W_MIN = 1
const EDGE_W_MAX = 8

// ── Helpers ───────────────────────────────────────────────────────────────

function bezierPath(x1: number, y1: number, x2: number, y2: number): string {
  const cx1 = x1 + (x2 - x1) * 0.5
  const cx2 = cx1
  return `M ${x1} ${y1} C ${cx1} ${y1} ${cx2} ${y2} ${x2} ${y2}`
}

type NodeColor = {
  fill: string
  stroke: string
  text: string
}

const STATUS_COLOR: Record<DestinationHealthStatus, NodeColor> = {
  healthy: { fill: "var(--color-success-50, #f0fdf4)", stroke: "var(--color-success-500, #22c55e)", text: "var(--color-success-700, #15803d)" },
  degraded: { fill: "var(--color-warning-50, #fffbeb)", stroke: "var(--color-warning-500, #f59e0b)", text: "var(--color-warning-700, #b45309)" },
  unhealthy: { fill: "var(--color-danger-50, #fef2f2)", stroke: "var(--color-danger-500, #ef4444)", text: "var(--color-danger-700, #b91c1c)" },
  disabled: { fill: "var(--color-surface-tertiary, #f1f5f9)", stroke: "var(--color-border, #e2e8f0)", text: "var(--color-text-tertiary, #94a3b8)" },
  unknown: { fill: "var(--color-surface-secondary, #f8fafc)", stroke: "var(--color-border, #e2e8f0)", text: "var(--color-text-secondary, #64748b)" },
}

function destColor(destId: string): NodeColor {
  if (destId === "__drop__") {
    return { fill: "var(--color-danger-50, #fef2f2)", stroke: "var(--color-danger-500, #ef4444)", text: "var(--color-danger-700, #b91c1c)" }
  }
  if (destId === "wazuh-default") {
    return { fill: "var(--color-surface-secondary, #f8fafc)", stroke: "var(--color-border, #e2e8f0)", text: "var(--color-text-secondary, #64748b)" }
  }
  return { fill: "var(--color-success-50, #f0fdf4)", stroke: "var(--color-success-500, #22c55e)", text: "var(--color-success-700, #15803d)" }
}

function routeColor(r: Route): NodeColor {
  if (!r.enabled) {
    return { fill: "var(--color-surface-tertiary, #f1f5f9)", stroke: "var(--color-border, #e2e8f0)", text: "var(--color-text-tertiary, #94a3b8)" }
  }
  if (r.action === "drop") {
    return { fill: "var(--color-danger-50, #fef2f2)", stroke: "var(--color-danger-500, #ef4444)", text: "var(--color-danger-700, #b91c1c)" }
  }
  if (r.unreachable) {
    return { fill: "var(--color-warning-50, #fffbeb)", stroke: "var(--color-warning-500, #f59e0b)", text: "var(--color-warning-700, #b45309)" }
  }
  return { fill: "var(--color-primary-100, #dbeafe)", stroke: "var(--color-primary-500, #3b82f6)", text: "var(--color-primary-700, #1d4ed8)" }
}

// ── Component ─────────────────────────────────────────────────────────────

interface TopologyProps {
  routes: Route[]
  /**
   * throughput/saúde por rota e destino. Opcional:
   * ausente ⇒ render idêntico ao legado (backward-compatible).
   */
  topology?: RoutingTopologyResponse
}

export const RoutingTopology: React.FC<TopologyProps> = ({ routes, topology }) => {
  const { t } = useTranslation("routing")
  const hasThroughput = !!topology

  const { routeNodes, destNodes, edges, svgH, svgW, summary } = useMemo(() => {
    // Lookups de throughput/saúde (vazios quando a prop é ausente).
    const routeTput = new Map(
      (topology?.routes ?? []).map((tr) => [tr.id, tr]),
    )
    const destInfo = new Map(
      (topology?.destinations ?? []).map((td) => [td.id, td]),
    )

    // Rotas habilitadas em ordem de prioridade
    const enabled = routes.slice().sort((a, b) => a.priority - b.priority)

    // Destinos únicos
    const destSet = new Map<string, { id: string; label: string }>()
    for (const r of enabled) {
      if (r.action === "drop") {
        if (!destSet.has("__drop__")) destSet.set("__drop__", { id: "__drop__", label: t("topology.dropLabel") })
      } else {
        const targets = r.destination_ids.length ? r.destination_ids : ["wazuh-default"]
        for (const d of targets) {
          if (!destSet.has(d)) destSet.set(d, { id: d, label: d === "wazuh-default" ? t("topology.wazuhDefaultLabel") : d })
        }
      }
    }

    const routeArr = enabled
    const destArr = Array.from(destSet.values())

    // Espaçamento vertical adaptativo: em topologias densas (muitos nós em uma
    // coluna) aumentamos o gap para reduzir sobreposição/ilegibilidade.
    const maxRows = Math.max(routeArr.length, destArr.length)
    const rowGap = maxRows > 8 ? ROW_GAP + 8 : ROW_GAP

    const routeH = routeArr.length * (NODE_H + rowGap) - rowGap
    const destH = destArr.length * (NODE_H + rowGap) - rowGap
    const contentH = Math.max(routeH, destH, NODE_H)

    const svgH = contentH + SVG_PAD_Y * 2
    const svgW = COL_X.dest + NODE_W + 20

    // Máximo de routed/min para normalizar a espessura das arestas.
    const maxRouted = Math.max(
      1,
      ...(topology?.routes ?? []).map((tr) => tr.routed_per_min || 0),
    )

    // Centros dos nós
    const routeNodes = routeArr.map((r, i) => ({
      route: r,
      tput: routeTput.get(r.id) ?? null,
      cx: COL_X.route,
      cy: SVG_PAD_Y + i * (NODE_H + rowGap) + NODE_H / 2,
      color: routeColor(r),
    }))

    const destNodes = destArr.map((d, i) => {
      const info = destInfo.get(d.id)
      // Cor por status quando há topologia; senão mantém o esquema legado.
      const color = hasThroughput && info ? STATUS_COLOR[info.status] : destColor(d.id)
      return {
        dest: d,
        info: info ?? null,
        cx: COL_X.dest,
        cy: SVG_PAD_Y + i * (NODE_H + rowGap) + NODE_H / 2,
        color,
      }
    })

    // Source node centrado na esquerda
    const sourceCY = svgH / 2

    // Arestas source → rota (rótulo = matched/min quando há throughput)
    const srcEdges = routeNodes.map((rn) => ({
      x1: COL_X.source + NODE_W,
      y1: sourceCY,
      x2: rn.cx,
      y2: rn.cy,
      dashed: false,
      width: 1.5,
      label: hasThroughput && rn.tput ? fmtRate(rn.tput.matched_per_min) : null,
      key: `src-${rn.route.id}`,
    }))

    // Arestas rota → destino (espessura ∝ routed/min; rótulo = routed/min)
    const rtEdges = routeNodes.flatMap((rn) => {
      const targets =
        rn.route.action === "drop"
          ? ["__drop__"]
          : rn.route.destination_ids.length
            ? rn.route.destination_ids
            : ["wazuh-default"]
      return targets.flatMap((tid) => {
        const dn = destNodes.find((d) => d.dest.id === tid)
        if (!dn) return []
        // Espessura proporcional, normalizada linearmente pelo máximo.
        const routed = rn.tput?.routed_per_min ?? 0
        const width =
          hasThroughput && rn.tput
            ? EDGE_W_MIN + (Math.min(routed, maxRouted) / maxRouted) * (EDGE_W_MAX - EDGE_W_MIN)
            : 1.5
        return [
          {
            x1: rn.cx + NODE_W,
            y1: rn.cy,
            x2: dn.cx,
            y2: dn.cy,
            dashed: !rn.route.is_final,
            width,
            label: hasThroughput && rn.tput ? fmtRate(routed) : null,
            key: `rt-${rn.route.id}-${tid}`,
          },
        ]
      })
    })

    const edges = [...srcEdges, ...rtEdges]

    const summary = [
      t("topology.summaryRoutes", { count: routeArr.length }),
      t("topology.summaryDestinations", { count: destArr.length }),
      t("topology.summaryDrops", { count: enabled.filter((r) => r.action === "drop").length }),
      t("topology.summaryFanOut", { count: enabled.filter((r) => !r.is_final).length }),
    ].join(", ")

    return { routeNodes, destNodes, edges, svgH, svgW, sourceCY, summary }
  }, [routes, topology, hasThroughput, t])

  const sourceCY = svgH / 2

  if (routes.length === 0) return null

  return (
    <Card padding="md" className="space-y-3 overflow-x-auto">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-text">{t("topology.title")}</h3>
        <div className="flex flex-wrap gap-2 text-xs">
          <span className="flex items-center gap-1 text-text-tertiary">
            <svg width="24" height="10" aria-hidden="true">
              <line x1="0" y1="5" x2="24" y2="5" stroke="currentColor" strokeWidth="1.5" />
            </svg>
            {t("topology.legendFinal")}
          </span>
          <span className="flex items-center gap-1 text-text-tertiary">
            <svg width="24" height="10" aria-hidden="true">
              <line x1="0" y1="5" x2="24" y2="5" stroke="currentColor" strokeWidth="1.5" strokeDasharray="4,3" />
            </svg>
            {t("topology.legendFanOut")}
          </span>
        </div>
      </div>

      <svg
        role="img"
        aria-label={hasThroughput ? t("topology.graphAriaLabelWithThroughput", { summary }) : t("topology.graphAriaLabel", { summary })}
        aria-describedby={hasThroughput ? "topology-throughput-desc" : undefined}
        width={svgW}
        height={svgH}
        className="min-w-full"
        style={{ fontFamily: "inherit" }}
      >
        {hasThroughput && (
          <desc id="topology-throughput-desc">
            {t("topology.throughputDescription")}
          </desc>
        )}
        {/* Arestas */}
        {edges.map((e) => (
          <g key={e.key}>
            <path
              d={bezierPath(e.x1, e.y1, e.x2, e.y2)}
              fill="none"
              stroke="var(--color-border, #e2e8f0)"
              strokeWidth={e.width}
              strokeOpacity={hasThroughput ? 0.85 : 1}
              strokeDasharray={e.dashed ? "5,4" : undefined}
            />
            {e.label != null && (() => {
              const txt = t("topology.perMinLabel", { value: e.label })
              const mx = (e.x1 + e.x2) / 2
              const my = (e.y1 + e.y2) / 2 - 3
              // Largura aproximada do rótulo (~5.2px por caractere a 9px) + padding.
              const rectW = txt.length * 5.2 + 8
              const rectH = 13
              return (
                <>
                  {/* Fundo semi-transparente para legibilidade sobre arestas densas. */}
                  <rect
                    x={mx - rectW / 2}
                    y={my - rectH + 3}
                    width={rectW}
                    height={rectH}
                    rx={3}
                    fill="var(--color-surface, #ffffff)"
                    fillOpacity={0.8}
                    pointerEvents="none"
                    data-testid={`edge-label-bg-${e.key}`}
                  />
                  <text
                    x={mx}
                    y={my}
                    textAnchor="middle"
                    fontSize="9"
                    fill="var(--color-text-tertiary, #94a3b8)"
                    aria-label={t("topology.perMinAria", { value: e.label })}
                    data-testid={`edge-label-${e.key}`}
                  >
                    {txt}
                  </text>
                </>
              )
            })()}
          </g>
        ))}

        {/* Nó Coleta (source) */}
        <rect
          x={COL_X.source}
          y={sourceCY - NODE_H / 2}
          width={NODE_W}
          height={NODE_H}
          rx={6}
          fill="var(--color-primary-100, #dbeafe)"
          stroke="var(--color-primary-500, #3b82f6)"
          strokeWidth="1.5"
        />
        <text
          x={COL_X.source + NODE_W / 2}
          y={sourceCY + 5}
          textAnchor="middle"
          fontSize="12"
          fontWeight="600"
          fill="var(--color-primary-700, #1d4ed8)"
        >
          {t("topology.sourceNode")}
        </text>

        {/* Nós de rota */}
        {routeNodes.map((rn) => (
          <g key={rn.route.id}>
            <rect
              x={rn.cx}
              y={rn.cy - NODE_H / 2}
              width={NODE_W}
              height={NODE_H}
              rx={6}
              fill={rn.color.fill}
              stroke={rn.color.stroke}
              strokeWidth="1.5"
            />
            <text
              x={rn.cx + NODE_W / 2}
              y={rn.cy - 4}
              textAnchor="middle"
              fontSize="10"
              fill="var(--color-text-tertiary, #94a3b8)"
            >
              #{rn.route.priority}
            </text>
            <text
              x={rn.cx + NODE_W / 2}
              y={rn.cy + 9}
              textAnchor="middle"
              fontSize="11"
              fontWeight="600"
              fill={rn.color.text}
            >
              {rn.route.name.length > 15 ? `${rn.route.name.slice(0, 13)}…` : rn.route.name}
            </text>
            {rn.route.canary_percent < 100 && (
              <text
                x={rn.cx + NODE_W - 4}
                y={rn.cy - NODE_H / 2 + 11}
                textAnchor="end"
                fontSize="9"
                fill="var(--color-warning-700, #b45309)"
              >
                {rn.route.canary_percent}%
              </text>
            )}
          </g>
        ))}

        {/* Nós de destino */}
        {destNodes.map((dn) => (
          <g key={dn.dest.id} data-testid={`topology-dest-${dn.dest.id}`}>
            <rect
              x={dn.cx}
              y={dn.cy - NODE_H / 2}
              width={NODE_W}
              height={NODE_H}
              rx={6}
              fill={dn.color.fill}
              stroke={dn.color.stroke}
              strokeWidth="1.5"
            />
            <text
              x={dn.cx + NODE_W / 2}
              y={dn.info && dn.info.eps != null ? dn.cy - 1 : dn.cy + 5}
              textAnchor="middle"
              fontSize="11"
              fontWeight="600"
              fill={dn.color.text}
            >
              {dn.dest.label.length > 16 ? `${dn.dest.label.slice(0, 14)}…` : dn.dest.label}
            </text>
            {dn.info && dn.info.eps != null && (
              <text
                x={dn.cx + NODE_W / 2}
                y={dn.cy + 11}
                textAnchor="middle"
                fontSize="9"
                fill="var(--color-text-tertiary, #94a3b8)"
                aria-label={t("topology.epsAria", { value: fmtRate(dn.info.eps) })}
                data-testid={`topology-dest-eps-${dn.dest.id}`}
              >
                {fmtRate(dn.info.eps)} {t("topology.epsSuffix")}
              </text>
            )}
          </g>
        ))}
      </svg>

      {/* Legenda de throughput + cores de status (só com a prop topology) */}
      {hasThroughput && (
        <div
          role="list"
          aria-label={t("topology.legendAriaLabel")}
          className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-text-tertiary"
          data-testid="topology-throughput-legend"
        >
          <span role="listitem" className="flex items-center gap-1">
            <svg width="28" height="10" aria-hidden="true">
              <line x1="0" y1="5" x2="28" y2="5" stroke="currentColor" strokeWidth={EDGE_W_MAX} strokeOpacity={0.85} />
            </svg>
            {t("topology.legendThroughput")}
          </span>
          <span role="listitem" className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: "var(--color-success-500, #22c55e)" }} aria-hidden="true" />
            {t("topology.legendHealthy")}
          </span>
          <span role="listitem" className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: "var(--color-warning-500, #f59e0b)" }} aria-hidden="true" />
            {t("topology.legendDegraded")}
          </span>
          <span role="listitem" className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: "var(--color-danger-500, #ef4444)" }} aria-hidden="true" />
            {t("topology.legendUnhealthy")}
          </span>
          <span role="listitem" className="flex items-center gap-1">
            <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: "var(--color-border, #e2e8f0)" }} aria-hidden="true" />
            {t("topology.legendDisabled")}
          </span>
        </div>
      )}

      {/* Legenda de rotas inalcançáveis ou desabilitadas */}
      {routes.some((r) => r.unreachable || !r.enabled) && (
        <div className="flex flex-wrap gap-2">
          {routes.filter((r) => r.unreachable).map((r) => (
            <Badge key={r.id} variant="warning" dot>
              {t("topology.routeUnreachableBadge", { priority: r.priority, name: r.name })}
            </Badge>
          ))}
          {routes.filter((r) => !r.enabled).map((r) => (
            <Badge key={r.id} variant="default">
              {t("topology.routeDisabledBadge", { priority: r.priority, name: r.name })}
            </Badge>
          ))}
        </div>
      )}
    </Card>
  )
}
