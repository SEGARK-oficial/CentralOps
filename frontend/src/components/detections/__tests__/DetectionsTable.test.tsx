/**
 * DetectionsTable — R2-5.3 (migração para DataTable com renderMobileCard).
 * Cobre: layout único por breakpoint, aria-describedby do cartão mobile
 * (item BAIXA), e a tabela desktop com o botão de regra.
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { DetectionsTable } from "@/components/detections/DetectionsTable"
import type { DetectionRead } from "@/types"

function makeDetection(overrides: Partial<DetectionRead> = {}): DetectionRead {
  return {
    id: 1,
    organization_id: 10,
    source: "scheduled_query",
    rule_id: "rule-001",
    rule_name: "Brute Force Detectado",
    severity_id: 4,
    status: "open",
    dedup_key: "org10:rule-001:hash1",
    count: 3,
    last_seen: "2026-06-22T08:00:00Z",
    created_at: "2026-06-20T10:00:00Z",
    ...overrides,
  }
}

/** Força `useMediaQuery` a resolver "é desktop" — o mock global de
 *  matchMedia (test/setup.ts) sempre devolve `matches:false`. */
function mockDesktopViewport(): () => void {
  const original = window.matchMedia
  window.matchMedia = ((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
  return () => {
    window.matchMedia = original
  }
}

describe("DetectionsTable — layout mobile (padrão em jsdom)", () => {
  it("renderiza cartão com nome acessível pelo título e descrição pelo resto (aria-describedby)", () => {
    const onRowClick = vi.fn()
    render(<DetectionsTable detections={[makeDetection()]} onRowClick={onRowClick} />)

    const card = screen.getByRole("button", { name: "Brute Force Detectado" })
    expect(card).toHaveAttribute("aria-describedby")
    // A descrição referencia elementos de verdade (fonte, severidade, status,
    // contagem) — não uma frase fixa embutida num aria-label só.
    const describedIds = card.getAttribute("aria-describedby")!.split(" ")
    expect(describedIds.length).toBeGreaterThanOrEqual(3)
    for (const id of describedIds) {
      expect(document.getElementById(id)).not.toBeNull()
    }

    fireEvent.click(card)
    expect(onRowClick).toHaveBeenCalledTimes(1)
  })

  it("não renderiza a tabela desktop junto (layout único)", () => {
    render(<DetectionsTable detections={[makeDetection()]} onRowClick={vi.fn()} />)
    expect(screen.queryByRole("table")).not.toBeInTheDocument()
  })
})

describe("DetectionsTable — layout desktop", () => {
  it("renderiza a tabela com o botão de regra, não o cartão", () => {
    const restore = mockDesktopViewport()
    try {
      const onRowClick = vi.fn()
      render(<DetectionsTable detections={[makeDetection()]} onRowClick={onRowClick} />)

      expect(screen.getByRole("table")).toBeInTheDocument()
      const ruleButton = screen.getByRole("button", { name: /Ver detalhes da detecção Brute Force Detectado/i })
      fireEvent.click(ruleButton)
      expect(onRowClick).toHaveBeenCalledTimes(1)
    } finally {
      restore()
    }
  })

  it("mostra severidade, fonte, status e ocorrências nas colunas", () => {
    const restore = mockDesktopViewport()
    try {
      render(
        <DetectionsTable
          detections={[makeDetection({ severity_id: 5, status: "closed", count: 7 })]}
          onRowClick={vi.fn()}
        />,
      )
      expect(screen.getByText("Crítica")).toBeInTheDocument()
      expect(screen.getByText("Fechada")).toBeInTheDocument()
      expect(screen.getByText("7")).toBeInTheDocument()
    } finally {
      restore()
    }
  })
})

describe("DetectionsTable — estados", () => {
  it("mostra EmptyState quando não há detecções", () => {
    render(<DetectionsTable detections={[]} onRowClick={vi.fn()} />)
    expect(screen.getByText("Nenhuma detecção encontrada")).toBeInTheDocument()
  })

  it("mostra o skeleton de loading (role=status) em vez de esconder a lista", () => {
    render(<DetectionsTable detections={[]} loading onRowClick={vi.fn()} />)
    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true")
  })
})
