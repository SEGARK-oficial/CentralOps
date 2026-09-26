/**
 * Testes de componente — TileGallery (grid de seleção, single + multiple).
 */
import { describe, it, expect, vi } from "vitest"
import { render, screen, fireEvent, within } from "@testing-library/react"
import { TileGallery, type Tile } from "@/components/shared/TileGallery"
import { Database } from "lucide-react"

const TILES: Tile[] = [
  { id: "sophos", label: "Sophos", description: "EDR/XDR", icon: <Database />, category: "EDR / XDR" },
  { id: "wazuh", label: "Wazuh", description: "SIEM open-source", icon: <Database />, category: "SIEM" },
  { id: "splunk", label: "Splunk", description: "SIEM", icon: <Database />, category: "SIEM" },
]

describe("TileGallery — render", () => {
  it("exibe um card por tile", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    const grid = screen.getByTestId("tile-grid")
    expect(within(grid).getAllByRole("radio")).toHaveLength(3)
  })

  it("mostra chips de categoria quando os tiles têm categoria", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    expect(screen.getByTestId("tile-categories")).toBeInTheDocument()
    expect(screen.getByTestId("tile-cat-siem")).toBeInTheDocument()
  })

  it("oculta chips quando nenhum tile tem categoria", () => {
    const noCat = TILES.map(({ category, ...t }) => t)
    render(<TileGallery tiles={noCat} value="" onChange={vi.fn()} />)
    expect(screen.queryByTestId("tile-categories")).not.toBeInTheDocument()
  })
})

describe("TileGallery — single select", () => {
  it("marca o selecionado via aria-checked e dispara onChange", () => {
    const onChange = vi.fn()
    render(<TileGallery tiles={TILES} value="wazuh" onChange={onChange} />)
    expect(screen.getByTestId("tile-card-wazuh")).toHaveAttribute("aria-checked", "true")
    expect(screen.getByTestId("tile-card-sophos")).toHaveAttribute("aria-checked", "false")
    fireEvent.click(screen.getByTestId("tile-card-sophos"))
    expect(onChange).toHaveBeenCalledWith("sophos")
  })
})

describe("TileGallery — multiple select", () => {
  it("usa role checkbox e reflete value[] como selecionados", () => {
    render(<TileGallery tiles={TILES} value={["sophos", "splunk"]} onChange={vi.fn()} multiple />)
    const grid = screen.getByTestId("tile-grid")
    expect(within(grid).getAllByRole("checkbox")).toHaveLength(3)
    expect(screen.getByTestId("tile-card-sophos")).toHaveAttribute("aria-checked", "true")
    expect(screen.getByTestId("tile-card-splunk")).toHaveAttribute("aria-checked", "true")
    expect(screen.getByTestId("tile-card-wazuh")).toHaveAttribute("aria-checked", "false")
  })

  it("dispara onChange (toggle) ao clicar", () => {
    const onChange = vi.fn()
    render(<TileGallery tiles={TILES} value={["sophos"]} onChange={onChange} multiple />)
    fireEvent.click(screen.getByTestId("tile-card-wazuh"))
    expect(onChange).toHaveBeenCalledWith("wazuh")
  })
})

describe("TileGallery — busca e filtro", () => {
  it("filtra por busca (label/descrição)", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId("tile-search"), { target: { value: "splunk" } })
    const grid = screen.getByTestId("tile-grid")
    expect(within(grid).getAllByRole("radio")).toHaveLength(1)
    expect(screen.getByTestId("tile-card-splunk")).toBeInTheDocument()
  })

  it("filtra por categoria via chip", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    fireEvent.click(screen.getByTestId("tile-cat-siem"))
    const grid = screen.getByTestId("tile-grid")
    expect(within(grid).getAllByRole("radio")).toHaveLength(2) // wazuh + splunk
  })

  it("mostra estado vazio quando a busca não casa", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    fireEvent.change(screen.getByTestId("tile-search"), { target: { value: "inexistente" } })
    expect(screen.getByTestId("tile-empty")).toBeInTheDocument()
  })
})

// ── A11Y-37: roving tabindex no radiogroup de categorias ────────────────────
describe("TileGallery — A11Y-37 (setas navegam/selecionam as categorias)", () => {
  it("o container de categorias é role=radiogroup (não role=group)", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    expect(screen.getByRole("radiogroup", { name: /filtrar por categoria/i })).toBeInTheDocument()
  })

  it("só a categoria selecionada tem tabIndex=0 — as demais são -1 (roving tabindex)", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    expect(screen.getByTestId("tile-cat-all")).toHaveAttribute("tabindex", "0")
    expect(screen.getByTestId("tile-cat-siem")).toHaveAttribute("tabindex", "-1")
    expect(screen.getByTestId("tile-cat-edr / xdr")).toHaveAttribute("tabindex", "-1")
  })

  it("ArrowRight seleciona a próxima categoria e move o foco pra ela", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    const todos = screen.getByTestId("tile-cat-all")
    todos.focus()
    fireEvent.keyDown(screen.getByTestId("tile-categories"), { key: "ArrowRight" })

    const edrXdr = screen.getByTestId("tile-cat-edr / xdr")
    expect(edrXdr).toHaveAttribute("aria-checked", "true")
    expect(edrXdr).toHaveAttribute("tabindex", "0")
    expect(document.activeElement).toBe(edrXdr)
    expect(todos).toHaveAttribute("tabindex", "-1")
  })

  it("ArrowLeft dá a volta (wrap) da primeira pra última categoria", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    const group = screen.getByTestId("tile-categories")
    fireEvent.keyDown(group, { key: "ArrowLeft" })
    // Categorias em ordem: Todos, EDR / XDR, SIEM — ArrowLeft de "Todos" (índice 0) vai pro último (SIEM).
    expect(screen.getByTestId("tile-cat-siem")).toHaveAttribute("aria-checked", "true")
  })

  it("End move a seleção pra última categoria", () => {
    render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
    fireEvent.keyDown(screen.getByTestId("tile-categories"), { key: "End" })
    expect(screen.getByTestId("tile-cat-siem")).toHaveAttribute("aria-checked", "true")
  })
})

describe("TileGallery — i18n dos rótulos padrão", () => {
  it("em inglês, rótulos padrão e a categoria 'todas' saem traduzidos (nada de PT fixo)", async () => {
    const { default: i18n } = await import("@/i18n")
    await i18n.changeLanguage("en")
    try {
      render(<TileGallery tiles={TILES} value="" onChange={vi.fn()} />)
      // Positivo: os rótulos em inglês existem.
      expect(screen.getByTestId("tile-cat-all")).toHaveTextContent("All")
      expect(screen.getByRole("radiogroup", { name: "Filter by category" })).toBeInTheDocument()
      expect(screen.getByTestId("tile-search")).toHaveAttribute("placeholder", "Search…")
      // Negativo: nenhum dos antigos defaults em PT sobrou.
      expect(screen.queryByText("Todos")).not.toBeInTheDocument()
      expect(screen.queryByPlaceholderText("Buscar…")).not.toBeInTheDocument()
      fireEvent.change(screen.getByTestId("tile-search"), { target: { value: "zzz-nada" } })
      expect(screen.getByTestId("tile-empty")).toHaveTextContent("No items found.")
    } finally {
      await i18n.changeLanguage("pt")
    }
  })
})
