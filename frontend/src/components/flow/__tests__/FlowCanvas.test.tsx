/**
 * Testes — FlowCanvas (Sankey + zoom/pan + partículas + drill-down).
 */
import { describe, it, expect, vi } from "vitest"
import { render, screen, fireEvent } from "@testing-library/react"
import { FlowCanvas } from "@/components/flow/FlowCanvas"
import type { FlowGraph as FlowGraphData } from "@/types"

const DATA: FlowGraphData = {
  generated_at: "2026-06-19T12:00:00Z",
  window_minutes: 60,
  sources: [
    { id: "s1", name: "Wazuh Prod", platform: "wazuh", status: "healthy", events_per_minute: 5400, eps: 90 },
    { id: "s2", name: "Sophos MDR", platform: "sophos", status: "degraded", events_per_minute: 2460, eps: 41 },
  ],
  routes: [
    {
      id: "r-sys",
      name: "catch-all",
      action: "route",
      destination_ids: ["wazuh-default"],
      matched_per_min: 8000,
      routed_per_min: 8000,
      drop_per_min: 0,
      enabled: true,
      is_system: true,
    },
    {
      id: "r1",
      name: "SIEM crítico",
      action: "route",
      destination_ids: ["d1"],
      matched_per_min: 3200,
      routed_per_min: 3100,
      drop_per_min: 100,
      enabled: true,
      is_system: false,
    },
    {
      id: "r3",
      name: "Noise drop",
      action: "drop",
      destination_ids: [],
      matched_per_min: 400,
      routed_per_min: 0,
      drop_per_min: 400,
      enabled: true,
      is_system: false,
    },
  ],
  destinations: [
    {
      id: "wazuh-default",
      name: "Wazuh (default)",
      kind: "syslog",
      status: "healthy",
      eps: 133,
      bytes_per_min: 1_000_000,
    },
    {
      id: "d1",
      name: "Splunk Prod",
      kind: "splunk_hec",
      status: "healthy",
      eps: 52,
      bytes_per_min: 800_000,
    },
  ],
  totals: { ingest_eps: 131, routed_per_min: 11100, drop_per_min: 500, delivered_eps: 185 },
}

describe("FlowCanvas", () => {
  it("renderiza um nó por fonte, rota e destino", () => {
    render(<FlowCanvas data={DATA} onSelectNode={vi.fn()} />)
    expect(screen.getAllByTestId(/^flow-source-/)).toHaveLength(2)
    expect(screen.getAllByTestId(/^flow-route-/)).toHaveLength(3)
    expect(screen.getAllByTestId(/^flow-dest-/)).toHaveLength(2)
  })

  it("renderiza nomes das fontes", () => {
    render(<FlowCanvas data={DATA} onSelectNode={vi.fn()} />)
    expect(screen.getByText("Wazuh Prod")).toBeInTheDocument()
    expect(screen.getByText("Sophos MDR")).toBeInTheDocument()
  })

  it("tem aria-label descritivo no SVG", () => {
    render(<FlowCanvas data={DATA} onSelectNode={vi.fn()} />)
    expect(
      screen.getByRole("img", { name: /2 fontes, 3 rotas, 2 destinos/i }),
    ).toBeInTheDocument()
  })

  it("clique em nó fonte chama onSelectNode com kind='source'", () => {
    const onSelect = vi.fn()
    render(<FlowCanvas data={DATA} onSelectNode={onSelect} />)
    const sourceNode = screen.getByTestId("flow-source-s1")
    fireEvent.click(sourceNode)
    expect(onSelect).toHaveBeenCalledOnce()
    expect(onSelect.mock.calls[0][0]).toMatchObject({ kind: "source" })
  })

  it("clique em nó rota chama onSelectNode com kind='route'", () => {
    const onSelect = vi.fn()
    render(<FlowCanvas data={DATA} onSelectNode={onSelect} />)
    const routeNode = screen.getByTestId("flow-route-r1")
    fireEvent.click(routeNode)
    expect(onSelect).toHaveBeenCalledOnce()
    expect(onSelect.mock.calls[0][0]).toMatchObject({ kind: "route" })
  })

  it("clique em nó destino chama onSelectNode com kind='dest'", () => {
    const onSelect = vi.fn()
    render(<FlowCanvas data={DATA} onSelectNode={onSelect} />)
    const destNode = screen.getByTestId("flow-dest-d1")
    fireEvent.click(destNode)
    expect(onSelect).toHaveBeenCalledOnce()
    expect(onSelect.mock.calls[0][0]).toMatchObject({ kind: "dest" })
  })

  it("Enter no nó dispara seleção (acessibilidade de teclado)", () => {
    const onSelect = vi.fn()
    render(<FlowCanvas data={DATA} onSelectNode={onSelect} />)
    const sourceNode = screen.getByTestId("flow-source-s1")
    fireEvent.keyDown(sourceNode, { key: "Enter" })
    expect(onSelect).toHaveBeenCalledOnce()
  })

  it("Space no nó dispara seleção (acessibilidade de teclado)", () => {
    const onSelect = vi.fn()
    render(<FlowCanvas data={DATA} onSelectNode={onSelect} />)
    const destNode = screen.getByTestId("flow-dest-wazuh-default")
    fireEvent.keyDown(destNode, { key: " " })
    expect(onSelect).toHaveBeenCalledOnce()
  })

  it("nós têm tabIndex=0 (focáveis por teclado)", () => {
    render(<FlowCanvas data={DATA} onSelectNode={vi.fn()} />)
    const sourceNode = screen.getByTestId("flow-source-s1")
    expect(sourceNode.getAttribute("tabindex")).toBe("0")
  })

  it("nós têm role='button'", () => {
    render(<FlowCanvas data={DATA} onSelectNode={vi.fn()} />)
    const sourceNode = screen.getByTestId("flow-source-s1")
    expect(sourceNode.getAttribute("role")).toBe("button")
  })

  it("controles de zoom estão acessíveis (aria-label)", () => {
    render(<FlowCanvas data={DATA} onSelectNode={vi.fn()} />)
    expect(screen.getByRole("button", { name: /Ampliar/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Reduzir/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Resetar/i })).toBeInTheDocument()
  })

  it("grafo com 0 nós renderiza SVG vazio sem crash", () => {
    const emptyData: FlowGraphData = {
      generated_at: "",
      window_minutes: 60,
      sources: [],
      routes: [],
      destinations: [],
      totals: { ingest_eps: 0, routed_per_min: 0, drop_per_min: 0, delivered_eps: 0 },
    }
    render(<FlowCanvas data={emptyData} onSelectNode={vi.fn()} />)
    expect(screen.getByRole("img")).toBeInTheDocument()
  })
})
