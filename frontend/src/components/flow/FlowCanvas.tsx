/**
 * FlowCanvas — mapa de fluxo dependency-free em SVG.
 *
 * Fontes → Roteamento → Destinos, com:
 * - Zoom (roda), pan (arraste) e FIT-TO-VIEW automático (nunca "quebra"/estoura,
 *   por mais nós que existam — o grafo é reescalado para caber no container).
 * - Ribbons Sankey com espessura ∝ throughput e GRADIENTE tingido por saúde.
 * - "Gather" fonte→roteamento com ORDEM PRESERVADA (sem convergir num ponto):
 *   as fitas entram na coluna de roteamento distribuídas por toda a sua altura,
 *   paralelas e sem cruzamentos — corrige o antigo funil-para-o-meio.
 * - FOCO+CONTEXTO: hover/foco num nó realça seus caminhos e esmaece o resto —
 *   é o que mantém o grafo legível quando há muitas fontes/rotas/destinos.
 * - AGRUPAMENTO: colunas muito longas colapsam os menores num nó "+N" expansível.
 * - Partículas SMIL (quantidade+velocidade ∝ throughput), respeitando
 *   prefers-reduced-motion. Ícones de marca por fonte/destino.
 * - Acessível: SVG role=img, nós role=button tabIndex=0, Enter/Espaço = drill-down.
 */
import type React from "react"
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ZoomInIcon, ZoomOutIcon, Maximize2Icon } from "lucide-react"
import { cn } from "@/lib/utils"
import { fmtRate } from "@/lib/fmt"
import { brandIconFor } from "@/lib/brand-icons"
import type {
  FlowGraph as FlowGraphData,
  FlowSource,
  TopologyRoute,
  TopologyDestination,
  FlowNodeStatus,
} from "@/types"

// ── Layout constants ───────────────────────────────────────────────────────
const PAD = 28
const NODE_W = 176
const NODE_H_MIN = 40
const NODE_H_MAX = 76
const NODE_H_BASE = 48
const V_GAP = 14
const GAP_X = 150
const HEADER_H = 34
const COL = {
  source: PAD,
  route: PAD + NODE_W + GAP_X,
  dest: PAD + 2 * (NODE_W + GAP_X),
}
const RIBBON_MIN_W = 2.5
const RIBBON_MAX_W = 26
const PARTICLE_R = 3
// Acima deste nº de nós numa coluna, os menores colapsam num nó "+N" (expansível).
const MAX_COL_NODES = 14
// Teto de arestas com partículas SMIL quando NÃO há foco (view densa/expandida):
// as `<animateMotion>` rodam contínuas no motor de render do browser. Acima disto,
// só as arestas de maior throughput animam — o resto fica estático (fitas seguem
// visíveis). Evita centenas de animações simultâneas na expansão total do grafo.
const MAX_PARTICLE_EDGES = 48

// ── Types ─────────────────────────────────────────────────────────────────
export type FlowNodeId =
  | { kind: "source"; node: FlowSource }
  | { kind: "route"; node: TopologyRoute }
  | { kind: "dest"; node: TopologyDestination }

type ColKind = "source" | "route" | "dest"
type NodeColor = { fill: string; stroke: string; text: string; dot: string; ribbon: string }

/** Nó de layout uniforme (real ou overflow) — evita uniões discriminadas frágeis. */
interface LNode {
  kind: ColKind
  id: string
  isOverflow: boolean
  weight: number // eps (source/dest ×1) ou routed_per_min (route)
  route: TopologyRoute | null // preenchido só p/ nós de rota reais (arestas rota→dest)
  top: number
  h: number
  cy: number
  color: NodeColor
  title: string
  subtitle: string
  tag?: string
  brand?: string
  onSelect: () => void
}

interface Edge {
  key: string
  fromId: string
  toId: string
  ribbonD: string
  centreD: string
  rate: number
  maxRate: number
  from: NodeColor
  to: NodeColor
  idle: boolean
}

// ── Color maps ────────────────────────────────────────────────────────────
const STATUS_COLOR: Record<FlowNodeStatus | "drop" | "system", NodeColor> = {
  healthy: { fill: "var(--color-success-50)", stroke: "var(--color-success-500)", text: "var(--color-success-700)", dot: "var(--color-success-500)", ribbon: "var(--color-success-500)" },
  degraded: { fill: "var(--color-warning-50)", stroke: "var(--color-warning-500)", text: "var(--color-warning-700)", dot: "var(--color-warning-500)", ribbon: "var(--color-warning-500)" },
  unhealthy: { fill: "var(--color-danger-50)", stroke: "var(--color-danger-500)", text: "var(--color-danger-700)", dot: "var(--color-danger-500)", ribbon: "var(--color-danger-500)" },
  unknown: { fill: "var(--color-surface-secondary)", stroke: "var(--color-border)", text: "var(--color-text-secondary)", dot: "var(--color-text-tertiary)", ribbon: "var(--color-text-tertiary)" },
  drop: { fill: "var(--color-danger-50)", stroke: "var(--color-danger-500)", text: "var(--color-danger-700)", dot: "var(--color-danger-500)", ribbon: "var(--color-danger-500)" },
  system: { fill: "var(--color-surface-secondary)", stroke: "var(--color-border)", text: "var(--color-text-secondary)", dot: "var(--color-text-tertiary)", ribbon: "var(--color-text-tertiary)" },
}

const ROUTE_COLOR: NodeColor = { fill: "var(--color-primary-50)", stroke: "var(--color-primary-500)", text: "var(--color-primary-700)", dot: "var(--color-primary-500)", ribbon: "var(--color-primary-500)" }

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

/** Sankey ribbon (bezier preenchido) entre banda origem e banda destino. */
function ribbonPath(x1: number, y1t: number, y1b: number, x2: number, y2t: number, y2b: number): string {
  const cx = x1 + (x2 - x1) * 0.5
  return [`M ${x1} ${y1t}`, `C ${cx} ${y1t} ${cx} ${y2t} ${x2} ${y2t}`, `L ${x2} ${y2b}`, `C ${cx} ${y2b} ${cx} ${y1b} ${x1} ${y1b}`, "Z"].join(" ")
}
/** Linha de centro (mpath das partículas). */
function centreLine(x1: number, y1c: number, x2: number, y2c: number): string {
  const cx = x1 + (x2 - x1) * 0.5
  return `M ${x1} ${y1c} C ${cx} ${y1c} ${cx} ${y2c} ${x2} ${y2c}`
}
function particleDur(rate: number, maxRate: number): number {
  if (rate <= 0 || maxRate <= 0) return 0
  return +(3.2 - clamp(rate / maxRate, 0, 1) * 2.1).toFixed(2)
}
function particleCount(rate: number, maxRate: number): number {
  if (rate <= 0 || maxRate <= 0) return 0
  return Math.max(1, Math.round(clamp(rate / maxRate, 0, 1) * 3))
}
/**
 * Id de gradiente por PAR DE CORES (não por aresta). As cores das fitas vêm de um
 * conjunto pequeno e fixo de tokens (STATUS_COLOR + ROUTE_COLOR) — dezenas/centenas
 * de arestas colapsam em ≤ ~49 gradientes únicos no `<defs>`. Sanitiza as CSS vars
 * p/ um id SVG válido e estável.
 */
function gradKey(from: string, to: string): string {
  return `grad-${from.replace(/[^a-zA-Z0-9]/g, "_")}__${to.replace(/[^a-zA-Z0-9]/g, "_")}`
}
/** Colapsa os itens de menor volume num overflow quando a coluna excede `cap`. */
interface Grouped<T> {
  visible: T[]
  overflow: { count: number; value: number } | null
}
function groupColumn<T>(items: T[], valueOf: (t: T) => number, cap: number, expanded: boolean): Grouped<T> {
  if (expanded || items.length <= cap) return { visible: items, overflow: null }
  const keep = new Set(
    items.map((it, i) => ({ i, v: valueOf(it) })).sort((a, b) => b.v - a.v).slice(0, cap - 1).map((x) => x.i),
  )
  const visible: T[] = []
  let count = 0
  let value = 0
  items.forEach((it, i) => {
    if (keep.has(i)) visible.push(it)
    else { count += 1; value += valueOf(it) }
  })
  return { visible, overflow: { count, value } }
}

const OVF = { source: "__ovf_src__", route: "__ovf_rt__", dest: "__ovf_dest__" } as const

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

  const [transform, setTransform] = useState({ tx: 0, ty: 0, scale: 1 })
  const dragging = useRef(false)
  const dragStart = useRef({ x: 0, y: 0, tx: 0, ty: 0 })
  const dragTarget = useRef({ tx: 0, ty: 0 })
  const rafId = useRef<number | null>(null)

  const [active, setActive] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Record<ColKind, boolean>>({ source: false, route: false, dest: false })

  const prefersReducedMotion =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches

  // ── Layout ──────────────────────────────────────────────────────────────
  const layout = useMemo(() => {
    const routesSorted = [...data.routes].sort((a, b) => Number(a.is_system) - Number(b.is_system))
    const gSrc = groupColumn(data.sources, (s) => s.eps, MAX_COL_NODES, expanded.source)
    const gRt = groupColumn(routesSorted, (r) => r.routed_per_min, MAX_COL_NODES, expanded.route)
    const gDest = groupColumn(data.destinations, (d) => d.eps ?? 0, MAX_COL_NODES, expanded.dest)

    // Pesos por coluna (visíveis + overflow).
    const wSrc = [...gSrc.visible.map((s) => s.eps), ...(gSrc.overflow ? [gSrc.overflow.value] : [])]
    const wRt = [...gRt.visible.map((r) => r.routed_per_min), ...(gRt.overflow ? [gRt.overflow.value] : [])]
    const wDest = [...gDest.visible.map((d) => d.eps ?? 0), ...(gDest.overflow ? [gDest.overflow.value] : [])]

    // Altura de cada nó ∝ seu volume (piso/teto), INDEPENDENTE da coluna — assim
    // svgH deriva das alturas REAIS e o conteúdo SEMPRE cabe (o fit reescala).
    const colHeights = (weights: number[]): number[] => {
      if (weights.length === 0) return []
      const mx = Math.max(0, ...weights)
      if (mx <= 0) return weights.map(() => NODE_H_BASE)
      return weights.map((w) => clamp(NODE_H_MIN + (w / mx) * (NODE_H_MAX - NODE_H_MIN), NODE_H_MIN, NODE_H_MAX))
    }
    const hSrc = colHeights(wSrc)
    const hRt = colHeights(wRt)
    const hDest = colHeights(wDest)
    const totalOf = (h: number[]) => h.reduce((a, b) => a + b, 0) + Math.max(0, h.length - 1) * V_GAP

    const contentH = Math.max(totalOf(hSrc), totalOf(hRt), totalOf(hDest), NODE_H_BASE)
    const svgH = contentH + PAD * 2 + HEADER_H
    const svgW = COL.dest + NODE_W + PAD

    const maxSrcEps = Math.max(0.0001, ...data.sources.map((s) => s.eps), gSrc.overflow?.value ?? 0)
    const maxRtRouted = Math.max(0.0001, ...data.routes.map((r) => r.routed_per_min), gRt.overflow?.value ?? 0)

    // Topo de cada nó — coluna centrada verticalmente dentro de contentH.
    const topsOf = (h: number[]): number[] => {
      let y = HEADER_H + PAD + (contentH - totalOf(h)) / 2
      return h.map((hi) => { const at = y; y += hi + V_GAP; return at })
    }
    const tSrc = topsOf(hSrc)
    const tRt = topsOf(hRt)
    const tDest = topsOf(hDest)

    const overflowLNode = (kind: ColKind, i: number, tops: number[], hs: number[], ovf: { count: number; value: number }): LNode => ({
      kind,
      id: OVF[kind],
      isOverflow: true,
      weight: ovf.value,
      route: null,
      top: tops[i],
      h: hs[i],
      cy: tops[i] + hs[i] / 2,
      color: STATUS_COLOR.unknown,
      title: t("flow.canvas.moreNodes", { count: ovf.count }),
      subtitle: t("flow.canvas.expandHint"),
      onSelect: () => setExpanded((e) => ({ ...e, [kind]: true })),
    })

    const sourceNodes: LNode[] = gSrc.visible.map((s, i) => ({
      kind: "source", id: s.id, isOverflow: false, weight: s.eps, route: null,
      top: tSrc[i], h: hSrc[i], cy: tSrc[i] + hSrc[i] / 2,
      color: STATUS_COLOR[s.status] ?? STATUS_COLOR.unknown,
      title: s.name, subtitle: `${fmtRate(s.eps * 60)}/min`, tag: s.platform, brand: s.platform,
      onSelect: () => onSelectNode({ kind: "source", node: s }),
    }))
    if (gSrc.overflow) sourceNodes.push(overflowLNode("source", gSrc.visible.length, tSrc, hSrc, gSrc.overflow))

    const routeNodes: LNode[] = gRt.visible.map((r, i) => ({
      kind: "route", id: r.id, isOverflow: false, weight: r.routed_per_min, route: r,
      top: tRt[i], h: hRt[i], cy: tRt[i] + hRt[i] / 2,
      color: r.is_system ? STATUS_COLOR.system : routeColorOf(r.action, r.enabled),
      title: r.is_system ? t("flow.canvas.catchAll") : r.name,
      subtitle: r.action === "drop" ? t("flow.canvas.drop") : `${fmtRate(r.routed_per_min)}/min`,
      tag: r.action === "drop" ? t("flow.canvas.drop") : r.enabled ? undefined : t("flow.canvas.off"),
      onSelect: () => onSelectNode({ kind: "route", node: r }),
    }))
    if (gRt.overflow) routeNodes.push(overflowLNode("route", gRt.visible.length, tRt, hRt, gRt.overflow))

    const destNodes: LNode[] = gDest.visible.map((d, i) => ({
      kind: "dest", id: d.id, isOverflow: false, weight: d.eps ?? 0, route: null,
      top: tDest[i], h: hDest[i], cy: tDest[i] + hDest[i] / 2,
      color: STATUS_COLOR[(d.status as FlowNodeStatus)] ?? STATUS_COLOR.unknown,
      title: d.name, subtitle: d.eps != null ? `${fmtRate(d.eps * 60)}/min` : d.kind, tag: d.kind, brand: d.kind,
      onSelect: () => onSelectNode({ kind: "dest", node: d }),
    }))
    if (gDest.overflow) destNodes.push(overflowLNode("dest", gDest.visible.length, tDest, hDest, gDest.overflow))

    // ── Gather fonte → roteamento (ORDEM PRESERVADA, sem funil) ───────────
    const rtTopEdge = routeNodes.length ? routeNodes[0].top : HEADER_H + PAD
    const rtBotEdge = routeNodes.length ? routeNodes[routeNodes.length - 1].top + routeNodes[routeNodes.length - 1].h : svgH - PAD
    const bandH = Math.max(rtBotEdge - rtTopEdge, RIBBON_MIN_W * Math.max(sourceNodes.length, 1))
    const srcWeights = sourceNodes.map((n) => clamp(n.weight / maxSrcEps, 0.06, 1))
    const wSum = srcWeights.reduce((a, b) => a + b, 0) || 1
    let bandCursor = rtTopEdge
    const srcEdges: Edge[] = sourceNodes.map((sn, i) => {
      const w = clamp(RIBBON_MIN_W + (sn.weight / maxSrcEps) * (RIBBON_MAX_W - RIBBON_MIN_W), RIBBON_MIN_W, RIBBON_MAX_W)
      const seg = (srcWeights[i] / wSum) * bandH
      const y2t = bandCursor
      const y2b = bandCursor + Math.max(seg, RIBBON_MIN_W)
      bandCursor = y2b
      const x1 = COL.source + NODE_W
      const x2 = COL.route
      return {
        key: `edge-src-${sn.id}`,
        fromId: sn.id,
        toId: "__routing__",
        ribbonD: ribbonPath(x1, sn.cy - w / 2, sn.cy + w / 2, x2, y2t, y2b),
        centreD: centreLine(x1, sn.cy, x2, (y2t + y2b) / 2),
        rate: sn.weight,
        maxRate: maxSrcEps,
        from: sn.color,
        to: ROUTE_COLOR,
        idle: sn.weight <= 0,
      }
    })

    // ── Rota → destino ────────────────────────────────────────────────────
    const destById = new Map(destNodes.map((d) => [d.id, d]))
    const resolveDest = (id: string) => destById.get(id) ?? (gDest.overflow ? destById.get(OVF.dest) : undefined)

    const rtEdges: Edge[] = []
    for (const rn of routeNodes) {
      const r = rn.route
      if (!r || r.action === "drop") continue
      const tids = r.destination_ids.length ? r.destination_ids : ["wazuh-default"]
      const seen = new Set<string>()
      const resolved: LNode[] = []
      for (const tid of tids) {
        const dn = resolveDest(tid)
        if (dn && !seen.has(dn.id)) { seen.add(dn.id); resolved.push(dn) }
      }
      if (resolved.length === 0) continue
      const sliceH = rn.h / resolved.length
      const perRate = r.routed_per_min / resolved.length
      resolved.forEach((dn, idx) => {
        const w = clamp(RIBBON_MIN_W + (r.routed_per_min / maxRtRouted) * (RIBBON_MAX_W - RIBBON_MIN_W), RIBBON_MIN_W, RIBBON_MAX_W)
        const x1 = COL.route + NODE_W
        const x2 = COL.dest
        const sliceC = rn.top + sliceH * idx + sliceH / 2
        rtEdges.push({
          key: `edge-rt-${rn.id}-${dn.id}`,
          fromId: rn.id,
          toId: dn.id,
          ribbonD: ribbonPath(x1, sliceC - w / 2, sliceC + w / 2, x2, dn.cy - w / 2, dn.cy + w / 2),
          centreD: centreLine(x1, sliceC, x2, dn.cy),
          rate: perRate,
          maxRate: maxRtRouted,
          from: rn.color,
          to: dn.color,
          idle: r.routed_per_min <= 0,
        })
      })
    }

    return { sourceNodes, routeNodes, destNodes, srcEdges, rtEdges, svgH, svgW }
  }, [data, expanded, onSelectNode, t])

  const { sourceNodes, routeNodes, destNodes, srcEdges, rtEdges, svgH, svgW } = layout
  // Altura de EXIBIÇÃO do canvas (o conteúdo é reescalado p/ caber nela via fit()).
  // Cresce com o grafo até um teto — grafos densos ficam menores mas pannable/zoom.
  const displayH = clamp(svgH, 340, 620)
  const allNodes = useMemo(() => [...sourceNodes, ...routeNodes, ...destNodes], [sourceNodes, routeNodes, destNodes])
  const allEdges = useMemo(() => [...srcEdges, ...rtEdges], [srcEdges, rtEdges])

  const focus = useMemo(() => {
    if (!active) return null
    const edgeKeys = new Set<string>()
    const nodeIds = new Set<string>([active])
    for (const e of allEdges) {
      if (e.fromId === active || e.toId === active) {
        edgeKeys.add(e.key)
        nodeIds.add(e.fromId)
        nodeIds.add(e.toId)
      }
    }
    return { edgeKeys, nodeIds }
  }, [active, allEdges])

  // Gradientes DEDUPLICADOS por par de cores — um `<linearGradient>` por par único
  // em vez de um por aresta. Estável (só depende da geometria/saúde do layout).
  const gradients = useMemo(() => {
    const m = new Map<string, { id: string; from: string; to: string }>()
    for (const e of allEdges) {
      const id = gradKey(e.from.ribbon, e.to.ribbon)
      if (!m.has(id)) m.set(id, { id, from: e.from.ribbon, to: e.to.ribbon })
    }
    return [...m.values()]
  }, [allEdges])

  // Quando NÃO há foco e o grafo é denso, limita as partículas às arestas de maior
  // throughput (null = sem teto, todas animam). Sob foco, o gate por-foco já reduz.
  const particleEdgeKeys = useMemo(() => {
    if (allEdges.length <= MAX_PARTICLE_EDGES) return null
    const top = [...allEdges]
      .sort((a, b) => b.rate / (b.maxRate || 1) - a.rate / (a.maxRate || 1))
      .slice(0, MAX_PARTICLE_EDGES)
    return new Set(top.map((e) => e.key))
  }, [allEdges])

  // ── Fit-to-view ───────────────────────────────────────────────────────────
  const fit = useCallback(() => {
    const el = containerRef.current
    if (!el) return
    const cw = el.clientWidth
    const ch = el.clientHeight || svgH + 24
    if (cw <= 0) return
    const scale = clamp(Math.min(cw / svgW, ch / svgH), 0.35, 1.4)
    setTransform({ tx: (cw - svgW * scale) / 2, ty: Math.max(0, (ch - svgH * scale) / 2), scale })
  }, [svgW, svgH])

  useLayoutEffect(() => {
    fit()
    const el = containerRef.current
    if (!el || typeof ResizeObserver === "undefined") return
    const ro = new ResizeObserver(() => fit())
    ro.observe(el)
    return () => ro.disconnect()
  }, [fit])

  // ── Wheel zoom ──────────────────────────────────────────────────────────
  const handleWheel = useCallback((e: React.WheelEvent<HTMLDivElement>) => {
    e.preventDefault()
    const rect = containerRef.current!.getBoundingClientRect()
    const mouseX = e.clientX - rect.left
    const mouseY = e.clientY - rect.top
    setTransform((prev) => {
      const factor = e.deltaY < 0 ? 1.1 : 0.9
      const newScale = clamp(prev.scale * factor, 0.35, 3)
      return {
        tx: mouseX - (mouseX - prev.tx) * (newScale / prev.scale),
        ty: mouseY - (mouseY - prev.ty) * (newScale / prev.scale),
        scale: newScale,
      }
    })
  }, [])

  const handleMouseDown = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if ((e.target as Element).closest("[data-node]")) return
    dragging.current = true
    dragStart.current = { x: e.clientX, y: e.clientY, tx: 0, ty: 0 }
    setTransform((prev) => {
      dragStart.current.tx = prev.tx; dragStart.current.ty = prev.ty
      dragTarget.current = { tx: prev.tx, ty: prev.ty }
      return prev
    })
  }, [])

  // Pan com throttle por ANIMATION FRAME: `mousemove` pode disparar >60×/s (trackpad
  // de alta taxa); coalescemos num único commit de estado por frame. Combinado com
  // os visuais memoizados (NodeVisual/EdgeVisual bailam quando só `transform` muda),
  // o custo por frame de arraste cai a O(nº de wrappers finos), não O(árvore inteira).
  const commitDrag = useCallback(() => {
    rafId.current = null
    setTransform((prev) => ({ ...prev, tx: dragTarget.current.tx, ty: dragTarget.current.ty }))
  }, [])

  const handleMouseMove = useCallback((e: React.MouseEvent<SVGSVGElement>) => {
    if (!dragging.current) return
    dragTarget.current = {
      tx: dragStart.current.tx + (e.clientX - dragStart.current.x),
      ty: dragStart.current.ty + (e.clientY - dragStart.current.y),
    }
    if (rafId.current !== null) return
    if (typeof requestAnimationFrame === "function") rafId.current = requestAnimationFrame(commitDrag)
    else commitDrag() // ambiente sem rAF (ex.: SSR/teste) — commit síncrono
  }, [commitDrag])

  const stopDrag = useCallback(() => {
    if (!dragging.current) return
    dragging.current = false
    // Garante que a posição FINAL do arraste seja aplicada mesmo que um frame
    // pendente seja cancelado (senão o último delta se perderia).
    if (rafId.current !== null) {
      if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafId.current)
      rafId.current = null
      setTransform((prev) => ({ ...prev, tx: dragTarget.current.tx, ty: dragTarget.current.ty }))
    }
  }, [])

  // Cancela qualquer frame de pan pendente ao desmontar.
  useEffect(() => () => {
    if (rafId.current !== null && typeof cancelAnimationFrame === "function") cancelAnimationFrame(rafId.current)
  }, [])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setActive(null); fit() }
    }
    window.addEventListener("keydown", handler)
    return () => window.removeEventListener("keydown", handler)
  }, [fit])

  const nodeOpacity = (id: string) => (focus && !focus.nodeIds.has(id) ? 0.16 : 1)
  const edgeOpacity = (key: string) => (focus ? (focus.edgeKeys.has(key) ? 0.9 : 0.06) : 0.5)

  const colX = (kind: ColKind) => (kind === "source" ? COL.source : kind === "route" ? COL.route : COL.dest)

  return (
    <div
      ref={containerRef}
      className={cn("relative overflow-hidden select-none", className)}
      onWheel={handleWheel}
      style={{ cursor: dragging.current ? "grabbing" : "grab", height: displayH }}
    >
      <div className="absolute right-3 top-3 z-10 flex flex-col gap-1" onMouseDown={(e) => e.stopPropagation()}>
        <ControlButton label={t("flow.canvas.zoomIn")} onClick={() => setTransform((p) => ({ ...p, scale: clamp(p.scale * 1.25, 0.35, 3) }))}>
          <ZoomInIcon size={14} />
        </ControlButton>
        <ControlButton label={t("flow.canvas.zoomOut")} onClick={() => setTransform((p) => ({ ...p, scale: clamp(p.scale * 0.8, 0.35, 3) }))}>
          <ZoomOutIcon size={14} />
        </ControlButton>
        <ControlButton label={t("flow.canvas.resetZoom")} onClick={fit}>
          <Maximize2Icon size={14} />
        </ControlButton>
      </div>

      <svg
        role="img"
        aria-label={t("flow.canvas.ariaLabel", { sources: sourceNodes.length, routes: routeNodes.length, destinations: destNodes.length })}
        width="100%"
        height={displayH}
        className="w-full"
        style={{ fontFamily: "inherit", display: "block" }}
        onMouseDown={handleMouseDown}
        onMouseMove={handleMouseMove}
        onMouseUp={stopDrag}
        onMouseLeave={() => { stopDrag(); setActive(null) }}
      >
        <defs>
          {gradients.map((g) => (
            <linearGradient key={g.id} id={g.id} x1="0" y1="0" x2="1" y2="0">
              <stop offset="0%" stopColor={g.from} />
              <stop offset="100%" stopColor={g.to} />
            </linearGradient>
          ))}
          <filter id="flow-node-shadow" x="-20%" y="-30%" width="140%" height="160%">
            <feDropShadow dx="0" dy="1" stdDeviation="2" floodColor="#0f172a" floodOpacity="0.18" />
          </filter>
        </defs>

        <g transform={`translate(${transform.tx} ${transform.ty}) scale(${transform.scale})`}>
          {([
            [t("flow.canvas.columns.sources"), COL.source, sourceNodes.length],
            [t("flow.canvas.columns.routing"), COL.route, routeNodes.length],
            [t("flow.canvas.columns.destinations"), COL.dest, destNodes.length],
          ] as [string, number, number][]).map(([label, x, count]) => (
            <text key={label} x={x + 4} y={18} fontSize="10" fontWeight="700" letterSpacing="0.09em" fill="var(--color-text-tertiary)">
              {label.toUpperCase()} · {count}
            </text>
          ))}

          {allEdges.map((e) => {
            const dur = particleDur(e.rate, e.maxRate)
            const animates =
              !prefersReducedMotion &&
              dur > 0 &&
              (!focus || focus.edgeKeys.has(e.key)) &&
              (particleEdgeKeys === null || particleEdgeKeys.has(e.key))
            const count = animates ? particleCount(e.rate, e.maxRate) : 0
            // O `<g>` externo carrega só o que muda no hover (opacity) — barato de
            // reconciliar. O desenho pesado (fita + partículas) fica no EdgeVisual
            // memoizado, que bail-out quando só o pan/zoom muda.
            return (
              <g key={e.key} data-testid={e.key} opacity={edgeOpacity(e.key)} style={{ transition: "opacity 160ms ease" }}>
                <EdgeVisual edge={e} gradId={gradKey(e.from.ribbon, e.to.ribbon)} count={count} dur={dur} />
              </g>
            )
          })}

          {allNodes.map((n) => (
            <SankeyNode
              key={`${n.kind}-${n.id}`}
              x={colX(n.kind)}
              node={n}
              opacity={nodeOpacity(n.id)}
              onHover={() => setActive(n.id)}
              onLeave={() => setActive((cur) => (cur === n.id ? null : cur))}
            />
          ))}
        </g>
      </svg>
    </div>
  )
}

// ── ControlButton ───────────────────────────────────────────────────────────
const ControlButton: React.FC<{ label: string; onClick: () => void; children: React.ReactNode }> = ({ label, onClick, children }) => (
  <button
    type="button"
    aria-label={label}
    className="flex h-8 w-8 items-center justify-center rounded-md border border-border bg-surface/90 text-text-secondary shadow-sm backdrop-blur hover:bg-surface-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
    onClick={onClick}
  >
    {children}
  </button>
)

// ── SankeyNode ──────────────────────────────────────────────────────────────
// Wrapper INTERATIVO fino: carrega testid, a11y, handlers e a `opacity` (que muda
// no hover/foco) — barato de reconciliar. O desenho pesado é delegado ao NodeVisual
// memoizado, que NÃO re-renderiza no hover (opacity) nem no pan (transform), pois
// suas props saem do `layout` memoizado (estáveis fora de mudança de dados).
const SankeyNode: React.FC<{ x: number; node: LNode; opacity: number; onHover: () => void; onLeave: () => void }> = ({ x, node, opacity, onHover, onLeave }) => {
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); node.onSelect() }
  }
  return (
    <g
      data-testid={`flow-${node.kind}-${node.id}`}
      data-node="true"
      tabIndex={0}
      role="button"
      aria-label={`${node.title} — ${node.subtitle}`}
      onClick={node.onSelect}
      onKeyDown={handleKeyDown}
      onMouseEnter={onHover}
      onMouseLeave={onLeave}
      onFocus={onHover}
      onBlur={onLeave}
      style={{ cursor: "pointer", outline: "none", transition: "opacity 160ms ease" }}
      opacity={opacity}
    >
      <NodeVisual x={x} node={node} />
    </g>
  )
}

// Desenho PURO do nó (rect + acento + ícone + textos + tag). Memoizado: só
// re-renderiza quando a geometria/rótulos mudam (novos dados) — nunca no hover
// nem no pan. É o que elimina o re-render O(árvore inteira) desses dois gestos.
const NodeVisual = memo(function NodeVisual({ x, node }: { x: number; node: LNode }) {
  const { top, h, color, title, subtitle, tag, brand, isOverflow } = node
  const cy = top + h / 2
  const rx = 9
  const hasIcon = !!brand && !isOverflow
  const textX = x + (hasIcon ? 34 : 15)
  return (
    <>
      <rect
        x={x}
        y={top}
        width={NODE_W}
        height={h}
        rx={rx}
        fill={color.fill}
        stroke={color.stroke}
        strokeWidth="1.5"
        strokeDasharray={isOverflow ? "5 4" : undefined}
        filter="url(#flow-node-shadow)"
      />
      {!isOverflow && <rect x={x} y={top} width={3.5} height={h} rx={2} fill={color.dot} />}

      {hasIcon ? (
        <foreignObject x={x + 10} y={cy - 9} width={18} height={18} style={{ pointerEvents: "none" }}>
          <div style={{ width: 18, height: 18, display: "flex", alignItems: "center", justifyContent: "center" }}>
            {brandIconFor(brand, { size: 16 })}
          </div>
        </foreignObject>
      ) : (
        !isOverflow && <circle cx={x + 15} cy={cy} r={4} fill={color.dot} />
      )}

      <text x={textX} y={cy - (h > NODE_H_BASE ? 6 : 4)} fontSize="11.5" fontWeight="600" fill={color.text} dominantBaseline="middle">
        {truncate(title, 17)}
      </text>
      <text x={textX} y={cy + (h > NODE_H_BASE ? 9 : 10)} fontSize="9.5" fill="var(--color-text-tertiary)" dominantBaseline="middle">
        {truncate(subtitle, 22)}
      </text>
      {tag && !isOverflow && (
        <text
          x={x + NODE_W - 10}
          y={cy + (h > NODE_H_BASE ? 9 : 10)}
          textAnchor="end"
          fontSize="8"
          fontWeight="700"
          letterSpacing="0.04em"
          fill="var(--color-text-tertiary)"
          opacity={0.75}
          dominantBaseline="middle"
        >
          {truncate(tag, 12).toUpperCase()}
        </text>
      )}
    </>
  )
})

// ── EdgeVisual ──────────────────────────────────────────────────────────────
// Desenho PURO da aresta: fita Sankey (gradiente DEDUPLICADO) + partículas SMIL.
// Memoizado — bail-out no pan/zoom (só `transform` muda) e no hover das arestas
// NÃO afetadas. `count`/`dur` são estáveis fora de mudança de foco/dados.
const EdgeVisual = memo(function EdgeVisual({ edge, gradId, count, dur }: { edge: Edge; gradId: string; count: number; dur: number }) {
  return (
    <>
      <path
        d={edge.ribbonD}
        fill={edge.idle ? "none" : `url(#${gradId})`}
        fillOpacity={0.5}
        stroke={edge.idle ? "var(--color-border)" : edge.to.ribbon}
        strokeWidth={edge.idle ? 1 : 0.5}
        strokeOpacity={edge.idle ? 0.6 : 0.25}
        strokeDasharray={edge.idle ? "4 5" : undefined}
      />
      {count > 0 && <path id={`${edge.key}-cl`} d={edge.centreD} fill="none" stroke="none" />}
      {Array.from({ length: count }, (_, pi) => (
        <circle key={pi} r={PARTICLE_R} fill={edge.to.dot} fillOpacity={0.9}>
          <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`${-(pi * (dur / count)).toFixed(2)}s`}>
            <mpath href={`#${edge.key}-cl`} />
          </animateMotion>
        </circle>
      ))}
    </>
  )
})
