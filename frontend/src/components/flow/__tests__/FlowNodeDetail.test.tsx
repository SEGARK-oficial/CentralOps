/**
 * Testes — FlowNodeDetail (painel lateral de drill-down).
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react"
import { FlowNodeDetail } from "@/components/flow/FlowNodeDetail"
import type { FlowNodeId } from "@/components/flow/FlowCanvas"
import type { FlowSource, TopologyRoute, TopologyDestination } from "@/types"

// Mock api
vi.mock("@/services/api", () => ({
  getDestinationTap: vi.fn().mockResolvedValue({
    destination_id: "d1",
    entries: [
      { timestamp: "2026-06-19T12:00:00Z", event_type: "alert", _redacted: false },
      { timestamp: "2026-06-19T11:59:00Z", event_type: "login", _redacted: true },
    ],
  }),
}))

const SOURCE: FlowSource = {
  id: "s1",
  name: "Wazuh Prod",
  platform: "wazuh",
  status: "healthy",
  events_per_minute: 5400,
  eps: 90,
}

const ROUTE: TopologyRoute = {
  id: "r1",
  name: "SIEM crítico",
  action: "route",
  destination_ids: ["d1"],
  matched_per_min: 3200,
  routed_per_min: 3100,
  drop_per_min: 100,
  enabled: true,
  is_system: false,
}

const DEST: TopologyDestination = {
  id: "d1",
  name: "Splunk Prod",
  kind: "splunk_hec",
  status: "healthy",
  eps: 52,
  bytes_per_min: 800_000,
}

describe("FlowNodeDetail", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("não renderiza quando node=null", () => {
    render(<FlowNodeDetail node={null} onClose={vi.fn()} />)
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("renderiza painel com role=dialog para nó fonte", () => {
    const node: FlowNodeId = { kind: "source", node: SOURCE }
    render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Wazuh Prod" })).toBeInTheDocument()
    // A badge da plataforma + row "Plataforma" aparecem; verificamos ao menos um
    expect(screen.getAllByText("wazuh").length).toBeGreaterThanOrEqual(1)
  })

  it("renderiza detalhes de rota com nome correto", () => {
    const node: FlowNodeId = { kind: "route", node: ROUTE }
    render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "SIEM crítico" })).toBeInTheDocument()
    // "Rota" label no kindLabel do header e na row "Tipo"
    expect(screen.getAllByText("Rota").length).toBeGreaterThanOrEqual(1)
  })

  it("renderiza detalhes de destino + carrega eventos do tap", async () => {
    const node: FlowNodeId = { kind: "dest", node: DEST }
    await act(async () => {
      render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    })
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Splunk Prod" })).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByText("alert")).toBeInTheDocument()
    })
    // Badge redacted deve aparecer
    expect(screen.getByText("redacted")).toBeInTheDocument()
  })

  it("ESC chama onClose", () => {
    const onClose = vi.fn()
    const node: FlowNodeId = { kind: "source", node: SOURCE }
    render(<FlowNodeDetail node={node} onClose={onClose} />)
    fireEvent.keyDown(document, { key: "Escape" })
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("botão fechar chama onClose", () => {
    const onClose = vi.fn()
    const node: FlowNodeId = { kind: "source", node: SOURCE }
    render(<FlowNodeDetail node={node} onClose={onClose} />)
    const closeBtn = screen.getByRole("button", { name: /Fechar painel/i })
    fireEvent.click(closeBtn)
    expect(onClose).toHaveBeenCalledOnce()
  })

  it("exibe badge de status saudável para fonte healthy", () => {
    const node: FlowNodeId = { kind: "source", node: SOURCE }
    render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    // "saudável" aparece na badge e na row Status
    expect(screen.getAllByText("saudável").length).toBeGreaterThanOrEqual(1)
  })

  it("exibe badge de status degradado para fonte degraded", () => {
    const degraded: FlowSource = { ...SOURCE, status: "degraded" }
    const node: FlowNodeId = { kind: "source", node: degraded }
    render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    // "degradado" aparece na badge e na row Status
    expect(screen.getAllByText("degradado").length).toBeGreaterThanOrEqual(1)
  })

  it("panel tem aria-modal=true", () => {
    const node: FlowNodeId = { kind: "source", node: SOURCE }
    render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    const dialog = screen.getByRole("dialog")
    expect(dialog.getAttribute("aria-modal")).toBe("true")
  })

  it("panel tem aria-label com nome do nó", () => {
    const node: FlowNodeId = { kind: "source", node: SOURCE }
    render(<FlowNodeDetail node={node} onClose={vi.fn()} />)
    const dialog = screen.getByRole("dialog")
    expect(dialog.getAttribute("aria-label")).toContain("Wazuh Prod")
  })
})
