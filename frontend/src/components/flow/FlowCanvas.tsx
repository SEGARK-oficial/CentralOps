/**
 * FlowCanvas — wraps FlowGraph SVG with zoom/pan (wheel + drag) and Sankey-style
 * ribbon edges with SMIL particle animation.
 *
 * Features:
 * - Zoom via mouse wheel (clamped 0.4–3), pan via drag on background
 * - Overlay controls: zoom in, zoom out, reset/fit
 * - Sankey ribbon edges (filled bezier paths, width proportional to flow)
 * - SMIL animateMotion particles along each edge, quantity+speed ∝ throughput
 * - Nodes sized proportionally to volume (eps / routed_per_min)
 * - Accessible: SVG role=img, nodes tabIndex=0, Enter triggers drill-down
 * - Respects prefers-reduced-motion
 */
import type React from "react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ZoomInIcon, ZoomOutIcon, Maximize2Icon } from "lucide-react"
import { cn } from "@/lib/utils"
import { fmtRate } from "@/lib/fmt"
import type {
  FlowGraph as FlowGraphData,
  FlowSource,
  TopologyRoute,
  TopologyDestination,
  FlowNodeStatus,
} from "@/types"

// ── Layout constants ───────────────────────────────────────────────────────
const PAD = 24
const NODE_W = 168
const NODE_H_MIN = 36
const NODE_H_MAX = 72
const NODE_H_BASE = 46
const V_GAP = 12
const GAP_X = 130
const COL = {
  source: PAD,
  route: PAD + NODE_W + GAP_X,
  dest: PAD + 2 * (NODE_W + GAP_X),
}
const RIBBON_MIN_W = 2
const RIBBON_MAX_W = 22
const PARTICLE_R = 3.5

// ── Types ─────────────────────────────────────────────────────────────────
export type FlowNodeId =
  | { kind: "source"; node: FlowSource }
  | { kind: "route"; node: TopologyRoute }
  | { kind: "dest"; node: TopologyDestination }

type NodeColor = { fill: string; stroke: string; text: string; dot: string; ribbon: string }

// ── Color maps ────────────────────────────────────────────────────────────
const STATUS_COLOR: Record<FlowNodeStatus | "drop" | "system", NodeColor> = {
  healthy: {
    fill: "var(--color-success-50)",
    stroke: "var(--color-success-500)",
    text: "var(--color-success-700)",
    dot: "var(--color-success-500)",
    ribbon: "var(--color-success-200)",
  },
  degraded: {
    fill: "var(--color-warning-50)",
    stroke: "var(--color-warning-500)",
    text: "var(--color-warning-700)",
    dot: "var(--color-warning-500)",
    ribbon: "var(--color-warning-200)",
  },
  unhealthy: {
    fill: "var(--color-danger-50)",
    stroke: "var(--color-danger-500)",
    text: "var(--color-danger-700)",
    dot: "var(--color-danger-500)",
    ribbon: "var(--color-danger-200)",
  },
  unknown: {
    fill: "var(--color-surface-secondary)",
    stroke: "var(--color-border)",
    text: "var(--color-text-secondary)",
    dot: "var(--color-text-tertiary)",
    ribbon: "var(--color-border)",
  },
  drop: {
    fill: "var(--color-danger-50)",
    stroke: "var(--color-danger-500)",
    text: "var(--color-danger-700)",
    dot: "var(--color-danger-500)",
    ribbon: "var(--color-danger-200)",
  },
  system: {
    fill: "var(--color-surface-secondary)",
    stroke: "var(--color-border)",
    text: "var(--color-text-secondary)",
    dot: "var(--color-text-tertiary)",
    ribbon: "var(--color-border)",
  },
}

const ROUTE_COLOR: NodeColor = {
  fill: "var(--color-primary-50)",
  stroke: "var(--color-primary-500)",
  text: "var(--color-primary-700)",
  dot: "var(--color-primary-500)",
  ribbon: "var(--color-primary-200)",
}

function routeColorOf(action: string, enabled: boolean): NodeColor {
  if (!enabled) return STATUS_COLOR.unknown
  if (action === "drop") return STATUS_COLOR.drop
  return ROUTE_COLOR
}

// ── Helpers ───────────────────────────────────────────────────────────────
function clamp(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v))
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}

/** Sankey ribbon path between two horizontal-band segments.
 *  (x1,y1top)→(x1,y1bot) is the source band; (x2,y2top)→(x2,y2bot) is the dest band.
 */
function ribbonPath(
  x1: number,
  y1top: number,
  y1bot: number,
  x2: number,
  y2top: number,
  y2bot: number,
): string {
  const cx = x1 + (x2 - x1) * 0.55
  return [
    `M ${x1} ${y1top}`,
    `C ${cx} ${y1top} ${cx} ${y2top} ${x2} ${y2top}`,
    `L ${x2} ${y2bot}`,
    `C ${cx} ${y2bot} ${cx} ${y1bot} ${x1} ${y1bot}`,
    "Z",
  ].join(" ")
}

/** Centre-line path (for animateMotion mpath). */
function centreLine(
  x1: number,
  y1top: number,
  y1bot: number,
  x2: number,
  y2top: number,
  y2bot: number,
): string {
  const y1c = (y1top + y1bot) / 2
  const y2c = (y2top + y2bot) / 2
  const cx = x1 + (x2 - x1) * 0.55
  return `M ${x1} ${y1c} C ${cx} ${y1c} ${cx} ${y2c} ${x2} ${y2c}`
}

/** Particle animation duration (seconds): more throughput → faster. */
function particleDur(rate: number, maxRate: number): number {
  if (rate <= 0 || maxRate <= 0) return 0
  const norm = clamp(rate / maxRate, 0, 1)
  return +(3.0 - norm * 2.0).toFixed(2) // 1.0s (fast) … 3.0s (slow)
}

/** Number of particles (1–4) proportional to normalised rate. */
function particleCount(rate: number, maxRate: number): number {
  if (rate <= 0 || maxRate <= 0) return 0
  const norm = clamp(rate / maxRate, 0, 1)
  return Math.max(1, Math.round(norm * 4))
}

/** Compute node heights proportional to volume, with a minimum. */
function computeHeights(values: number[], totalH: number, count: number): number[] {
  const totalGap = (count - 1) * V_GAP
  const availableH = totalH - PAD * 2 - totalGap
  const sum = values.reduce((a, b) => a + b, 0)
  if (sum <= 0 || count === 0) return values.map(() => NODE_H_BASE)

  // raw proportional heights
  const raw = values.map((v) => (v / sum) * availableH)
  // clamp to [NODE_H_MIN, NODE_H_MAX]
  const clamped = raw.map((h) => clamp(h, NODE_H_MIN, NODE_H_MAX))
  return clamped
}

// ── Props ─────────────────────────────────────────────────────────────────
interface FlowCanvasProps {
  data: FlowGraphData
  onSelectNode: (node: FlowNodeId) => void
  className?: string
}

// ── Component ─────────────────────────────────────────────────────────────
export const FlowCanvas: React.FC<FlowCanvasProps> = ({ data, onSelectNode, className }) => {
  const { t } = useTranslation("dashboard")
  const containerRef = useRef<HTMLDivElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  // Zoom / pan state
  const [transform, setTransform] = useState({ tx: 0, ty: 0, scale: 1 })
  const dragging = useRef(false)
  const dragStart = useRef({ x: 0, y: 0, tx: 0, ty: 0 })

  const prefersReducedMotion =
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches

  // ── Layout ──────────────────────────────────────────────────────────────
  const layout = useMemo(() => {
    const sources = data.sources
    const routes = [...data.routes].sort((a, b) => Number(a.is_system) - Number(b.is_system))
    const dests = data.destinations

    const maxCount = Math.max(sources.length, routes.length, dests.length, 1)
    // Canvas height fitted to the tallest column
    const minColH = maxCount * (NODE_H_BASE + V_GAP) - V_GAP + PAD * 2
    const svgH = Math.max(minColH, 280)
    const svgW = COL.dest + NODE_W + PAD

    const maxSrcEps = Math.max(0.0001, ...sources.map((s) => s.eps))
    const maxRtRouted = Math.max(0.0001, ...routes.map((r) => r.routed_per_min))
    const maxDestEps = Math.max(0.0001, ...dests.map((d) => d.eps ?? 0))

    // Heights for each column
    const srcHeights = computeHeights(sources.map((s) => s.eps), svgH, sources.length)
    const rtHeights = computeHeights(routes.map((r) => r.routed_per_min), svgH, routes.length)
    const destHeights = computeHeights(dests.map((d) => d.eps ?? 1), svgH, dests.length)

    // Top-Y for each column (centred vertically)
    function topYs(heights: number[]): number[] {
      const totalH = heights.reduce((a, b) => a + b, 0) + (heights.length - 1) * V_GAP
      const startY = PAD + (svgH - PAD * 2 - totalH) / 2
      const tops: number[] = []
      let y = startY
      for (const h of heights) {
        tops.push(y)
        y += h + V_GAP
      }
      return tops
    }

    const srcTops = topYs(srcHeights)
    const rtTops = topYs(rtHeights)
    const destTops = topYs(destHeights)

    // Node objects
    const sourceNodes = sources.map((s, i) => ({
      s,
      top: srcTops[i],
      h: srcHeights[i],
      cy: srcTops[i] + srcHeights[i] / 2,
      color: STATUS_COLOR[s.status] ?? STATUS_COLOR.unknown,
    }))
    const routeNodes = routes.map((r, i) => ({
      r,
      top: rtTops[i],
      h: rtHeights[i],
      cy: rtTops[i] + rtHeights[i] / 2,
      color: r.is_system ? STATUS_COLOR.system : routeColorOf(r.action, r.enabled),
    }))
    const destNodes = dests.map((d, i) => ({
      d,
      top: destTops[i],
      h: destHeights[i],
      cy: destTops[i] + destHeights[i] / 2,
      color: STATUS_COLOR[(d.status as FlowNodeStatus)] ?? STATUS_COLOR.unknown,
    }))

    // Route centre Y (for source fan-in)
    const routesCenterY =
      routeNodes.length ? routeNodes.reduce((a, n) => a + n.cy, 0) / routeNodes.length : svgH / 2

    // ── Ribbon edges: source → routing band ─────────────────────────────
    // We fan all sources into the collective routing band (centred on routesCenterY)
    const totalSrcRibbonH = sourceNodes.reduce((a, n) => a + clamp(n.s.eps / maxSrcEps, 0.1, 1) * RIBBON_MAX_W, 0)
    let srcRibbonCursor = routesCenterY - totalSrcRibbonH / 2

    const srcEdges = sourceNodes.map((sn) => {
      const w = clamp(
        RIBBON_MIN_W + (sn.s.eps / maxSrcEps) * (RIBBON_MAX_W - RIBBON_MIN_W),
        RIBBON_MIN_W,
        RIBBON_MAX_W,
      )
      const x1 = COL.source + NODE_W
      const y1top = sn.cy - w / 2
      const y1bot = sn.cy + w / 2
      const x2 = COL.route
      const y2top = srcRibbonCursor
      const y2bot = srcRibbonCursor + w
      srcRibbonCursor += w + 1.5
      const edgeId = `edge-src-${sn.s.id}`
      return {
        key: edgeId,
        edgeId,
        ribbonD: ribbonPath(x1, y1top, y1bot, x2, y2top, y2bot),
        centreD: centreLine(x1, y1top, y1bot, x2, y2top, y2bot),
        rate: sn.s.eps,
        maxRate: maxSrcEps,
        color: sn.color,
        w,
      }
    })

    // ── Ribbon edges: route → destination ───────────────────────────────
    // For each route, partition its right border by destination_ids (even split)
    const rtEdges: Array<{
      key: string
      edgeId: string
      ribbonD: string
      centreD: string
      rate: number
      maxRate: number
      color: NodeColor
      w: number
      label: string | null
    }> = []

    for (const rn of routeNodes) {
      if (rn.r.action === "drop") continue
      const tids = rn.r.destination_ids.length ? rn.r.destination_ids : ["wazuh-default"]
      const validTids = tids.filter((tid) => destNodes.some((dn) => dn.d.id === tid))
      if (validTids.length === 0) continue

      const ratePerDest = rn.r.routed_per_min / validTids.length
      // partition right border of route node evenly
      const sliceH = rn.h / validTids.length

      validTids.forEach((tid, idx) => {
        const dn = destNodes.find((d) => d.d.id === tid)
        if (!dn) return
        const w = clamp(
          RIBBON_MIN_W + (rn.r.routed_per_min / maxRtRouted) * (RIBBON_MAX_W - RIBBON_MIN_W),
          RIBBON_MIN_W,
          RIBBON_MAX_W,
        )
        const x1 = COL.route + NODE_W
        const sliceCenter = rn.top + sliceH * idx + sliceH / 2
        const y1top = sliceCenter - w / 2
        const y1bot = sliceCenter + w / 2
        const x2 = COL.dest
        const y2top = dn.cy - w / 2
        const y2bot = dn.cy + w / 2
        const edgeId = `edge-rt-${rn.r.id}-${tid}`
        rtEdges.push({
          key: edgeId,
          edgeId,
          ribbonD: ribbonPath(x1, y1top, y1bot, x2, y2top, y2bot),
          centreD: centreLine(x1, y1top, y1bot, x2, y2top, y2bot),
          rate: ratePerDest,
          maxRate: maxRtRouted,
          color: rn.color,
          w,
          label: rn.r.routed_per_min > 0 ? `${fmtRate(rn.r.routed_per_min)}/min` : null,
        })
      })
    }

    return {
      sourceNodes,
      routeNodes,
      destNodes,
      srcEdges,
      rtEdges,
      svgH,
      svgW,
      maxSrcEps,
      maxRtRouted,
      maxDestEps,
    }
  }, [data])

  const { sourceNodes, routeNodes, destNodes, srcEdges, rtEdges, svgH, svgW } = layout

  // ── Wheel zoom ──────────────────────────────────────────────────────────
  const handleWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault()
    const rect = containerRef.current!.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const mouseY = e.clientY - rect.top
    setTransform((prev) => {
      const factor = e.deltaY < 0 ? 1.1 : 0.9
      const newScale = clamp(prev.scale * factor, 0.4, 3)
      // Zoom toward cursor
      const tx = mouseX - (mouseX - prev.tx) * (newScale / prev.scale)
      const ty = mouseY - (mouseY - prev.ty) * (newScale / prev.scale)
      return { tx, ty, scale: newScale }
    })
  }, [])

  // ── Drag pan ────────────────────────────────────────────────────────────
  const handleMouseDown = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    // Only drag on the SVG background (not on nodes)
    if ((e.target as Element).closest("[data-node]")) return
    dragging.current = true
    dragStart.current = {
      x: e.clientX,
      y: e.clientY,
      tx: 0,
      ty: 0,
    }
    // store current tx/ty
    setTransform((prev) => {
      dragStart.current.tx = prev.tx
      dragStart.current.ty = prev.ty
      return prev
    })
  }, [])

  const handleMouseMove = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if (!dragging.current) return
    const dx = e.clientX - dragStart.current.x
    const dy = e.clientY - dragStart.current.y
    setTransform((prev) => ({
      ...prev,
      tx: dragStart.current.tx + dx,
      ty: dragStart.current.ty + dy,
    }))
  }, [])

  const stopDrag = useCallback(() => {
    dragging.current = false
  }, [])

  // ── Keyboard: ESC resets transform ──────────────────────────────────────
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setTransform({ tx: 0, ty: 0, scale: 1 })
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [])

  const allEdges = [...srcEdges, ...rtEdges]

  return (
    <div
      ref={containerRef}
      className={cn("relative overflow-hidden select-none", className)}
      onWheel={handleWheel}
      style={{ cursor: dragging.current ? "grabbing" : "grab" }}
    >
      {/* Overlay controls */}
      <div
        className="absolute right-3 top-3 z-10 flex flex-col gap-1"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <button
          type="button"
          aria-label={t("flow.canvas.zoomIn")}
          className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-surface text-text-secondary shadow-sm hover:bg-surface-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
          onClick={() =>
            setTransform((p) => ({ ...p, scale: clamp(p.scale * 1.25, 0.4, 3) }))
          }
        >
          <ZoomInIcon size={14} />
        </button>
        <button
          type="button"
          aria-label={t("flow.canvas.zoomOut")}
          className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-surface text-text-secondary shadow-sm hover:bg-surface-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
          onClick={() =>
            setTransform((p) => ({ ...p, scale: clamp(p.scale * 0.8, 0.4, 3) }))
          }
        >
          <ZoomOutIcon size={14} />
        </button>
        <button
          type="button"
          aria-label={t("flow.canvas.resetZoom")}
          className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-surface text-text-secondary shadow-sm hover:bg-surface-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
          onClick={() => setTransform({ tx: 0, ty: 0, scale: 1 })}
        >
          <Maximize2Icon size={14} />
        </button>
      </div>

      <style>{`
        @keyframes flowDash { to { stroke-dashoffset: -28; } }
        @media (prefers-reduced-motion: no-preference) {
          .flow-particle { animation: particleMove linear infinite; }
        }
      `}</style>

      <svg
        ref={svgRef}
        role="img"
        aria-label={t("flow.canvas.ariaLabel", { sources: sourceNodes.length, routes: routeNodes.length, destinations: destNodes.length })}
        width={svgW}
        height={svgH}
        viewBox={`0 0 ${svgW} ${svgH}`}
        className="w-full"
        style={{ fontFamily: "inherit", minHeight: `${svgH}px` }}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={stopDrag}
        onMouseLeave={stopDrag}
      >
        <g transform={`translate(${transform.tx} ${transform.ty}) scale(${transform.scale})`}>
          {/* Column headers */}
          {(
            [
              [t("flow.canvas.columns.sources"), COL.source],
              [t("flow.canvas.columns.routing"), COL.route],
              [t("flow.canvas.columns.destinations"), COL.dest],
            ] as [string, number][]
          ).map(([label, x]) => (
            <text
              key={label}
              x={x + NODE_W / 2}
              y={14}
              textAnchor="middle"
              fontSize="9"
              fontWeight="700"
              letterSpacing="0.08em"
              fill="var(--color-text-tertiary)"
            >
              {label}
            </text>
          ))}

          {/* Ribbon edges + particles (rendered behind nodes) */}
          {allEdges.map((e) => {
            const dur = particleDur(e.rate, e.maxRate)
            const count = !prefersReducedMotion && dur > 0 ? particleCount(e.rate, e.maxRate) : 0
            return (
              <g key={e.key}>
                {/* Ribbon fill */}
                <path
                  id={e.edgeId}
                  d={e.ribbonD}
                  fill={e.color.ribbon}
                  fillOpacity={0.45}
                  stroke={e.color.stroke}
                  strokeWidth={0.5}
                  strokeOpacity={0.3}
                />
                {/* Centre-line path for particle motion (hidden) */}
                {count > 0 && (
                  <path
                    id={`${e.edgeId}-cl`}
                    d={e.centreD}
                    fill="none"
                    stroke="none"
                  />
                )}
                {/* Particles */}
                {Array.from({ length: count }, (_, pi) => (
                  <circle
                    key={pi}
                    r={PARTICLE_R}
                    fill={e.color.dot}
                    fillOpacity={0.85}
                  >
                    <animateMotion
                      dur={`${dur}s`}
                      repeatCount="indefinite"
                      begin={`${-(pi * (dur / count)).toFixed(2)}s`}
                    >
                      <mpath href={`#${e.edgeId}-cl`} />
                    </animateMotion>
                  </circle>
                ))}
              </g>
            )
          })}

          {/* Source nodes */}
          {sourceNodes.map((n) => (
            <SankeyNode
              key={n.s.id}
              x={COL.source}
              top={n.top}
              h={n.h}
              color={n.color}
              title={n.s.name}
              // Unidade unificada do fluxo: eventos/min (fonte vem em EPS → ×60).
              subtitle={`${fmtRate(n.s.eps * 60)}/min`}
              tag={n.s.platform}
              testid={`flow-source-${n.s.id}`}
              onSelect={() => onSelectNode({ kind: "source", node: n.s })}
            />
          ))}

          {/* Route nodes */}
          {routeNodes.map((n) => (
            <SankeyNode
              key={n.r.id}
              x={COL.route}
              top={n.top}
              h={n.h}
              color={n.color}
              title={n.r.is_system ? t("flow.canvas.catchAll") : n.r.name}
              subtitle={
                n.r.action === "drop"
                  ? t("flow.canvas.drop")
                  : `${fmtRate(n.r.routed_per_min)}/min`
              }
              tag={n.r.action === "drop" ? t("flow.canvas.drop") : n.r.enabled ? undefined : t("flow.canvas.off")}
              testid={`flow-route-${n.r.id}`}
              onSelect={() => onSelectNode({ kind: "route", node: n.r })}
            />
          ))}

          {/* Destination nodes */}
          {destNodes.map((n) => (
            <SankeyNode
              key={n.d.id}
              x={COL.dest}
              top={n.top}
              h={n.h}
              color={n.color}
              title={n.d.name}
              subtitle={
                n.d.eps != null ? `${fmtRate(n.d.eps * 60)}/min` : n.d.kind
              }
              tag={n.d.kind}
              testid={`flow-dest-${n.d.id}`}
              onSelect={() => onSelectNode({ kind: "dest", node: n.d })}
            />
          ))}
        </g>
      </svg>
    </div>
  )
}

// ── SankeyNode ─────────────────────────────────────────────────────────────
interface SankeyNodeProps {
  x: number
  top: number
  h: number
  color: NodeColor
  title: string
  subtitle: string
  tag?: string
  testid: string
  onSelect: () => void
}

const SankeyNode: React.FC<SankeyNodeProps> = ({
  x,
  top,
  h,
  color,
  title,
  subtitle,
  tag,
  testid,
  onSelect,
}) => {
  const cy = top + h / 2
  const rx = 8

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault()
      onSelect()
    }
  }

  return (
    <g
      data-testid={testid}
      data-node="true"
      tabIndex={0}
      role="button"
      aria-label={`${title} — ${subtitle}`}
      onClick={onSelect}
      onKeyDown={handleKeyDown}
      style={{ cursor: "pointer", outline: "none" }}
    >
      {/* Focus ring (SVG rect as proxy) */}
      <rect
        x={x - 2}
        y={top - 2}
        width={NODE_W + 4}
        height={h + 4}
        rx={rx + 2}
        fill="none"
        stroke="var(--color-primary-500)"
        strokeWidth={2}
        strokeOpacity={0}
        className="focus-ring-proxy"
      />
      <rect
        x={x}
        y={top}
        width={NODE_W}
        height={h}
        rx={rx}
        fill={color.fill}
        stroke={color.stroke}
        strokeWidth="1.5"
      />
      {/* Status dot */}
      <circle cx={x + 14} cy={cy} r={4} fill={color.dot} />
      {/* Title */}
      <text
        x={x + 26}
        y={cy - (h > NODE_H_BASE ? 6 : 4)}
        fontSize="11.5"
        fontWeight="600"
        fill={color.text}
        dominantBaseline="middle"
      >
        {truncate(title, 16)}
      </text>
      {/* Subtitle */}
      <text
        x={x + 26}
        y={cy + (h > NODE_H_BASE ? 8 : 10)}
        fontSize="9.5"
        fill="var(--color-text-tertiary)"
        dominantBaseline="middle"
      >
        {truncate(subtitle, 22)}
      </text>
      {/* Tag */}
      {tag && (
        <text
          x={x + NODE_W - 8}
          y={top + 11}
          textAnchor="end"
          fontSize="8"
          fill="var(--color-text-tertiary)"
        >
          {truncate(tag, 14)}
        </text>
      )}
    </g>
  )
}
