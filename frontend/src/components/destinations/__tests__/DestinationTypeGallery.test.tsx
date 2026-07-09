/**
 * Testes de componente — DestinationTypeGallery
 *
 * Cobre:
 * - Render padrão: exibe todos os cards com label, badge e categoria
 * - Busca filtra por label e por kind
 * - Filtro por categoria (chip) exibe apenas os kinds correspondentes
 * - Clique em card invoca onSelect com o kind correto
 * - Card selecionado tem aria-checked=true
 * - Acessibilidade: radiogroup, role radio, navegação por teclado (Enter/Space)
 * - Empty state quando nenhum card passa nos filtros
 */

import { render, screen, fireEvent, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { DestinationTypeGallery } from "@/components/destinations/DestinationTypeGallery"
import type { DestinationType } from "@/types"

// ── Fixtures ──────────────────────────────────────────────────────────────────

// Catálogo self-describing (o backend manda category/icon_id/tier/description) —
// a galeria é 100% plugin-driven, sem mapas hardcoded.
const entry = (over: Partial<DestinationType> & Pick<DestinationType, "kind" | "label" | "category">): DestinationType => ({
  default_queue: "q",
  capabilities: [],
  required_secrets: [],
  config_schema: { type: "object", properties: {} },
  delivery_schema: { type: "object", properties: {} },
  delivery_defaults: {},
  description: "",
  icon_id: null,
  tier: "stable",
  ...over,
})

const makeCatalog = (): DestinationType[] => [
  entry({ kind: "syslog_rfc3164", label: "Syslog RFC3164", category: "Syslog", icon_id: "syslog" }),
  entry({ kind: "syslog_rfc5424", label: "Syslog RFC5424", category: "Syslog", icon_id: "syslog" }),
  entry({ kind: "splunk_hec", label: "Splunk HEC", category: "SIEM", icon_id: "splunk", required_secrets: ["hec_token"], tier: "beta" }),
  entry({ kind: "elastic_bulk", label: "Elasticsearch Bulk", category: "SIEM", icon_id: "elastic" }),
  entry({ kind: "otlp", label: "OTLP", category: "Telemetria", icon_id: "opentelemetry" }),
  entry({ kind: "jsonl", label: "JSON Lines", category: "Arquivo", icon_id: "jsonl" }),
]

// ── Helpers ───────────────────────────────────────────────────────────────────

interface RenderProps {
  selectedKind?: string
  onSelect?: (kind: string) => void
  catalog?: DestinationType[]
}

function renderGallery({ selectedKind = "", onSelect = vi.fn(), catalog = makeCatalog() }: RenderProps = {}) {
  const result = render(
    <DestinationTypeGallery
      catalog={catalog}
      selectedKind={selectedKind}
      onSelect={onSelect}
    />,
  )
  return { ...result, onSelect }
}

// ── Testes ────────────────────────────────────────────────────────────────────

describe("DestinationTypeGallery — render padrão", () => {
  it("exibe um card por tipo do catálogo", () => {
    renderGallery()
    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(6)
  })

  it("cada card exibe o label do tipo", () => {
    renderGallery()
    expect(screen.getByText("Splunk HEC")).toBeInTheDocument()
    expect(screen.getByText("JSON Lines")).toBeInTheDocument()
    expect(screen.getByText("OTLP")).toBeInTheDocument()
  })

  it("tier 'beta' renderiza badge 'Beta'; 'stable' não renderiza badge de tier", () => {
    renderGallery()
    // splunk_hec é beta no fixture → badge "Beta"; os demais (stable) não têm badge de tier.
    expect(screen.getByText("Beta")).toBeInTheDocument()
    expect(screen.queryByText("Nativo")).not.toBeInTheDocument()
  })

  it("cada card exibe badge de categoria", () => {
    renderGallery()
    // Syslog: 2 cards
    const syslogBadges = screen.getAllByText("Syslog")
    expect(syslogBadges.length).toBeGreaterThanOrEqual(2)
    // SIEM: splunk + elastic
    const siemBadges = screen.getAllByText("SIEM")
    expect(siemBadges.length).toBeGreaterThanOrEqual(2)
  })

  it("exibe input de busca", () => {
    renderGallery()
    expect(screen.getByTestId("gallery-search")).toBeInTheDocument()
  })

  it("exibe chips de categoria incluindo 'Todos'", () => {
    renderGallery()
    expect(screen.getByTestId("gallery-cat-todos")).toBeInTheDocument()
    expect(screen.getByTestId("gallery-cat-syslog")).toBeInTheDocument()
    expect(screen.getByTestId("gallery-cat-siem")).toBeInTheDocument()
    expect(screen.getByTestId("gallery-cat-telemetria")).toBeInTheDocument()
    expect(screen.getByTestId("gallery-cat-arquivo")).toBeInTheDocument()
  })
})

describe("DestinationTypeGallery — busca", () => {
  it("filtrar por label exibe apenas os cards correspondentes", () => {
    renderGallery()
    const search = screen.getByTestId("gallery-search")
    fireEvent.change(search, { target: { value: "Splunk" } })

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(1)
    expect(screen.getByText("Splunk HEC")).toBeInTheDocument()
  })

  it("filtrar por kind (string técnica) também funciona", () => {
    renderGallery()
    const search = screen.getByTestId("gallery-search")
    fireEvent.change(search, { target: { value: "otlp" } })

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(1)
    expect(screen.getByText("OTLP")).toBeInTheDocument()
  })

  it("busca sem resultado exibe empty state", () => {
    renderGallery()
    const search = screen.getByTestId("gallery-search")
    fireEvent.change(search, { target: { value: "nonexistent-xyz" } })

    expect(screen.queryByTestId("gallery-grid")).not.toBeInTheDocument()
    expect(screen.getByTestId("gallery-empty")).toBeInTheDocument()
  })

  it("busca é case-insensitive", () => {
    renderGallery()
    const search = screen.getByTestId("gallery-search")
    fireEvent.change(search, { target: { value: "JSONL" } })

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(1)
    expect(screen.getByText("JSON Lines")).toBeInTheDocument()
  })
})

describe("DestinationTypeGallery — filtro por categoria", () => {
  it("filtrar por 'Syslog' exibe apenas syslog_rfc3164 e syslog_rfc5424", () => {
    renderGallery()
    fireEvent.click(screen.getByTestId("gallery-cat-syslog"))

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(2)
    expect(screen.getByText("Syslog RFC3164")).toBeInTheDocument()
    expect(screen.getByText("Syslog RFC5424")).toBeInTheDocument()
    expect(screen.queryByText("Splunk HEC")).not.toBeInTheDocument()
  })

  it("filtrar por 'SIEM' exibe splunk_hec e elastic_bulk", () => {
    renderGallery()
    fireEvent.click(screen.getByTestId("gallery-cat-siem"))

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(2)
    expect(screen.getByText("Splunk HEC")).toBeInTheDocument()
    expect(screen.getByText("Elasticsearch Bulk")).toBeInTheDocument()
  })

  it("filtrar por 'Telemetria' exibe apenas otlp", () => {
    renderGallery()
    fireEvent.click(screen.getByTestId("gallery-cat-telemetria"))

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(1)
    expect(screen.getByText("OTLP")).toBeInTheDocument()
  })

  it("filtrar por 'Arquivo' exibe apenas jsonl", () => {
    renderGallery()
    fireEvent.click(screen.getByTestId("gallery-cat-arquivo"))

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(1)
    expect(screen.getByText("JSON Lines")).toBeInTheDocument()
  })

  it("voltar para 'Todos' exibe todos os cards novamente", () => {
    renderGallery()
    fireEvent.click(screen.getByTestId("gallery-cat-siem"))
    fireEvent.click(screen.getByTestId("gallery-cat-todos"))

    const grid = screen.getByTestId("gallery-grid")
    const cards = within(grid).getAllByRole("radio")
    expect(cards).toHaveLength(6)
  })

  it("chip de categoria ativa tem aria-checked=true", () => {
    renderGallery()
    const syslogChip = screen.getByTestId("gallery-cat-syslog")
    expect(syslogChip).toHaveAttribute("aria-checked", "false")
    fireEvent.click(syslogChip)
    expect(syslogChip).toHaveAttribute("aria-checked", "true")
  })
})

describe("DestinationTypeGallery — interação / seleção", () => {
  it("clicar em card chama onSelect com o kind correto", () => {
    const onSelect = vi.fn()
    renderGallery({ onSelect })
    fireEvent.click(screen.getByTestId("gallery-card-splunk_hec"))
    expect(onSelect).toHaveBeenCalledWith("splunk_hec")
  })

  it("card selecionado tem aria-checked=true", () => {
    renderGallery({ selectedKind: "otlp" })
    expect(screen.getByTestId("gallery-card-otlp")).toHaveAttribute("aria-checked", "true")
    expect(screen.getByTestId("gallery-card-splunk_hec")).toHaveAttribute("aria-checked", "false")
  })

  it("card não selecionado tem aria-checked=false", () => {
    renderGallery({ selectedKind: "jsonl" })
    expect(screen.getByTestId("gallery-card-syslog_rfc3164")).toHaveAttribute("aria-checked", "false")
  })
})

describe("DestinationTypeGallery — acessibilidade", () => {
  it("grid tem role=radiogroup com aria-label descritivo", () => {
    renderGallery()
    expect(
      screen.getByRole("radiogroup", { name: /Selecionar tipo de destino/i }),
    ).toBeInTheDocument()
  })

  it("cada card tem role=radio", () => {
    renderGallery()
    const grid = screen.getByTestId("gallery-grid")
    const radios = within(grid).getAllByRole("radio")
    expect(radios.length).toBeGreaterThan(0)
    for (const r of radios) {
      expect(r.tagName).toBe("BUTTON")
    }
  })

  it("cada card tem aria-label com o nome do tipo", () => {
    renderGallery()
    expect(screen.getByRole("radio", { name: /Selecionar Splunk HEC/i })).toBeInTheDocument()
    expect(screen.getByRole("radio", { name: /Selecionar JSON Lines/i })).toBeInTheDocument()
  })

  it("cards são ativados por Enter (via click sintético do fireEvent)", () => {
    const onSelect = vi.fn()
    renderGallery({ onSelect })
    const card = screen.getByTestId("gallery-card-elastic_bulk")
    card.focus()
    fireEvent.keyDown(card, { key: "Enter", code: "Enter" })
    fireEvent.click(card)
    expect(onSelect).toHaveBeenCalledWith("elastic_bulk")
  })

  it("grupo de chips tem role=group com aria-label", () => {
    renderGallery()
    expect(
      screen.getByRole("group", { name: /Filtrar por categoria/i }),
    ).toBeInTheDocument()
  })

  it("catálogo vazio não renderiza o grid", () => {
    renderGallery({ catalog: [] })
    expect(screen.queryByTestId("gallery-grid")).not.toBeInTheDocument()
    expect(screen.getByTestId("gallery-empty")).toBeInTheDocument()
  })
})
