/**
 * RoutingTopology tests (flow-view com throughput).
 *
 * Cobre:
 * - Render legado (sem prop `topology`): nós/arestas, SEM rótulos de throughput
 *   nem legenda de espessura (backward-compatible).
 * - Render com `topology`: rótulos eventos/min nas arestas, cor do destino por
 *   `status`, EPS no nó de destino, legenda de throughput.
 */

import { render, screen } from "@testing-library/react"
import { describe, it, expect, beforeAll } from "vitest"
import { RoutingTopology } from "@/components/routes/RoutingTopology"
import i18n from "@/i18n"
import type { Route, RoutingTopologyResponse } from "@/types"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

function makeRoute(over: Partial<Route> = {}): Route {
  return {
    id: "route-1",
    name: "Sophos para Splunk",
    priority: 1,
    condition: { vendor: "sophos" },
    action: "route",
    destination_ids: ["dest-splunk-001"],
    is_final: true,
    canary_percent: 100,
    transform_ref: null,
    pii_redaction: null,
    enabled: true,
    organization_id: null,
    created_at: "2024-01-01T00:00:00Z",
    updated_at: "2024-01-01T00:00:00Z",
    unreachable: false,
    ...over,
  }
}

const ROUTES: Route[] = [
  makeRoute(),
  makeRoute({
    id: "route-2",
    name: "Firewall para Syslog",
    priority: 2,
    condition: { category: "firewall" },
    destination_ids: ["dest-syslog-001"],
  }),
]

const TOPOLOGY: RoutingTopologyResponse = {
  destinations: [
    { id: "dest-splunk-001", name: "Splunk HEC", kind: "splunk_hec", status: "healthy", eps: 120, bytes_per_min: 4096 },
    { id: "dest-syslog-001", name: "Syslog SIEM", kind: "syslog", status: "unhealthy", eps: 3, bytes_per_min: 64 },
  ],
  routes: [
    {
      id: "route-1",
      name: "Sophos para Splunk",
      action: "route",
      destination_ids: ["dest-splunk-001"],
      matched_per_min: 1500,
      routed_per_min: 1500,
      drop_per_min: 0,
      enabled: true,
      is_system: false,
    },
    {
      id: "route-2",
      name: "Firewall para Syslog",
      action: "route",
      destination_ids: ["dest-syslog-001"],
      matched_per_min: 90,
      routed_per_min: 90,
      drop_per_min: 0,
      enabled: true,
      is_system: false,
    },
  ],
}

describe("RoutingTopology — render legado (sem prop topology)", () => {
  it("renderiza o grafo com nós de rota e destino, sem rótulos de throughput", () => {
    render(<RoutingTopology routes={ROUTES} />)

    // Grafo acessível presente
    const svg = screen.getByRole("img", { name: /grafo de roteamento/i })
    expect(svg).toBeInTheDocument()
    // Sem sufixo "(com throughput)" no aria-label
    expect(svg).toHaveAttribute("aria-label", expect.not.stringContaining("throughput"))

    // Nós de destino renderizados
    expect(screen.getByTestId("topology-dest-dest-splunk-001")).toBeInTheDocument()
    expect(screen.getByTestId("topology-dest-dest-syslog-001")).toBeInTheDocument()

    // Sem rótulos de aresta nem legenda/EPS de throughput
    expect(screen.queryByTestId("topology-throughput-legend")).not.toBeInTheDocument()
    expect(screen.queryByTestId("topology-dest-eps-dest-splunk-001")).not.toBeInTheDocument()
    expect(document.querySelector('[data-testid^="edge-label-"]')).toBeNull()
  })

  it("retorna null quando não há rotas", () => {
    const { container } = render(<RoutingTopology routes={[]} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe("RoutingTopology — com prop topology (throughput)", () => {
  it("renderiza rótulos de throughput nas arestas e EPS por destino", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const svg = screen.getByRole("img", { name: /grafo de roteamento.*com throughput/i })
    expect(svg).toBeInTheDocument()

    // Rótulos de eventos/min nas arestas (source→rota matched, rota→destino routed)
    const edgeLabels = Array.from(document.querySelectorAll('[data-testid^="edge-label-"]'))
    expect(edgeLabels.length).toBeGreaterThan(0)
    const labelText = edgeLabels.map((el) => el.textContent).join(" ")
    expect(labelText).toMatch(/1\.5k\/min/) // 1500 → 1.5k
    expect(labelText).toMatch(/90\/min/)

    // EPS por nó de destino
    expect(screen.getByTestId("topology-dest-eps-dest-splunk-001")).toHaveTextContent("120 EPS")
    expect(screen.getByTestId("topology-dest-eps-dest-syslog-001")).toHaveTextContent("3.0 EPS")

    // Legenda de throughput presente
    expect(screen.getByTestId("topology-throughput-legend")).toBeInTheDocument()
  })

  it("colore o nó de destino pelo status (healthy=verde, unhealthy=danger)", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const splunkNode = screen.getByTestId("topology-dest-dest-splunk-001")
    const splunkRect = splunkNode.querySelector("rect")!
    expect(splunkRect.getAttribute("stroke")).toContain("--color-success-500")

    const syslogNode = screen.getByTestId("topology-dest-dest-syslog-001")
    const syslogRect = syslogNode.querySelector("rect")!
    expect(syslogRect.getAttribute("stroke")).toContain("--color-danger-500")
  })

  it("espessura da aresta rota→destino é proporcional ao routed_per_min", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    // Aresta da rota-1 (1500/min, máximo) deve ser mais grossa que rota-2 (90/min).
    const edge1 = document.querySelector(
      '[data-testid="edge-label-rt-route-1-dest-splunk-001"]',
    )!.parentElement!.querySelector("path")!
    const edge2 = document.querySelector(
      '[data-testid="edge-label-rt-route-2-dest-syslog-001"]',
    )!.parentElement!.querySelector("path")!

    const w1 = parseFloat(edge1.getAttribute("stroke-width")!)
    const w2 = parseFloat(edge2.getAttribute("stroke-width")!)
    expect(w1).toBeGreaterThan(w2)
  })
})

describe("RoutingTopology — acessibilidade do throughput", () => {
  it("rótulos de aresta (eventos/min) têm aria-label descritivo", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const label = document.querySelector(
      '[data-testid="edge-label-rt-route-1-dest-splunk-001"]',
    )!
    expect(label.getAttribute("aria-label")).toBe("1.5k eventos por minuto")
  })

  it("o EPS do nó de destino tem aria-label 'eventos por segundo'", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const eps = screen.getByTestId("topology-dest-eps-dest-splunk-001")
    expect(eps.getAttribute("aria-label")).toBe("120 eventos por segundo")
  })

  it("renderiza um rect de fundo atrás de cada rótulo de aresta", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const labels = Array.from(document.querySelectorAll('[data-testid^="edge-label-rt-"]'))
    expect(labels.length).toBeGreaterThan(0)

    const bgs = Array.from(document.querySelectorAll('[data-testid^="edge-label-bg-"]'))
    // Há um fundo para CADA rótulo de aresta visível.
    const totalEdgeLabels = document.querySelectorAll('[data-testid^="edge-label-"]:not([data-testid^="edge-label-bg-"])').length
    expect(bgs.length).toBe(totalEdgeLabels)

    const bg = document.querySelector(
      '[data-testid="edge-label-bg-rt-route-1-dest-splunk-001"]',
    )!
    expect(bg.tagName.toLowerCase()).toBe("rect")
    expect(bg.getAttribute("pointer-events")).toBe("none")
    expect(parseFloat(bg.getAttribute("fill-opacity")!)).toBeLessThan(1)
    expect(bg.getAttribute("fill")).toContain("--color-surface")
  })

  it("NÃO renderiza rects de fundo de rótulo no modo legado (sem topology)", () => {
    render(<RoutingTopology routes={ROUTES} />)
    expect(document.querySelector('[data-testid^="edge-label-bg-"]')).toBeNull()
  })

  it("a legenda de throughput é uma lista acessível (role=list + listitem)", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const legend = screen.getByTestId("topology-throughput-legend")
    expect(legend).toHaveAttribute("role", "list")
    expect(legend.getAttribute("aria-label")).toMatch(/legenda/i)

    const items = legend.querySelectorAll('[role="listitem"]')
    expect(items.length).toBe(5) // espessura + 4 status
  })

  it("o SVG descreve a codificação via <desc> referenciado por aria-describedby", () => {
    render(<RoutingTopology routes={ROUTES} topology={TOPOLOGY} />)

    const svg = screen.getByRole("img", { name: /grafo de roteamento/i })
    const descId = svg.getAttribute("aria-describedby")
    expect(descId).toBeTruthy()

    const desc = document.getElementById(descId!)
    expect(desc).toBeInTheDocument()
    expect(desc!.tagName.toLowerCase()).toBe("desc")
    expect(desc!.textContent).toMatch(/espessura/i)
    expect(desc!.textContent).toMatch(/saudável/i)
    expect(desc!.textContent).toMatch(/indisponível/i)
  })

  it("no modo legado o SVG não tem <desc> de throughput", () => {
    render(<RoutingTopology routes={ROUTES} />)
    const svg = screen.getByRole("img", { name: /grafo de roteamento/i })
    expect(svg.getAttribute("aria-describedby")).toBeNull()
    expect(document.getElementById("topology-throughput-desc")).toBeNull()
  })
})
