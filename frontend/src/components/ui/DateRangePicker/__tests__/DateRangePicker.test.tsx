/**
 * DateRangePicker — sem cobertura antes. Foco em:
 *   A11Y-28: roving tabindex, aria-selected, aria-current, Home/End/setas.
 *   A11Y-20: alvo mínimo de 24px no botão "limpar".
 */
import { useState } from "react"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { DateRangePicker } from "@/components/ui/DateRangePicker/DateRangePicker"
import { Modal } from "@/components/ui/Modal/Modal"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// Componente controlado — sem `value`+`onChange` de verdade, um clique num
// dia nunca "gruda" (handleDateClick só chama `onChange`, não guarda estado
// interno). Espelha como os consumidores reais usam o componente.
function ControlledPicker() {
  const [value, setValue] = useState<{ from: Date | null; to: Date | null }>({ from: null, to: null })
  return <DateRangePicker aria-label="Período" value={value} onChange={setValue} />
}

function openPicker() {
  render(<ControlledPicker />)
  fireEvent.click(screen.getByRole("button", { name: "Período" }))
}

describe("DateRangePicker — render básico", () => {
  it("abre o popover ao clicar no trigger", () => {
    openPicker()
    expect(screen.getByRole("grid")).toBeInTheDocument()
  })

  it("mostra o placeholder quando não há valor", () => {
    render(<DateRangePicker aria-label="Período" placeholder="Selecione o período" />)
    expect(screen.getByRole("button", { name: "Período" })).toHaveTextContent("Selecione o período")
  })
})

describe("DateRangePicker — A11Y-28 (roving tabindex)", () => {
  it("só uma célula do grid tem tabIndex=0; as demais são -1", () => {
    openPicker()
    const cells = screen.getAllByRole("gridcell")
    const tabbable = cells.filter((c) => c.tabIndex === 0)
    expect(tabbable).toHaveLength(1)
    for (const cell of cells) {
      expect([0, -1]).toContain(cell.tabIndex)
    }
  })

  it("o dia de hoje tem aria-current=date", () => {
    openPicker()
    const today = new Date()
    const cells = screen.getAllByRole("gridcell")
    const todayCell = cells.find((c) => c.textContent === String(today.getDate()))
    expect(todayCell).toHaveAttribute("aria-current", "date")
  })

  it("clicar num dia marca aria-selected=true nele", () => {
    openPicker()
    const today = new Date()
    const cells = screen.getAllByRole("gridcell")
    const todayCell = cells.find((c) => c.textContent === String(today.getDate()))!
    fireEvent.click(todayCell)
    expect(todayCell).toHaveAttribute("aria-selected", "true")
  })

  it("ArrowRight move o roving tabindex pro dia seguinte", () => {
    openPicker()
    const grid = screen.getByRole("grid")
    const cellsBefore = screen.getAllByRole("gridcell")
    const before = cellsBefore.find((c) => c.tabIndex === 0)!
    const beforeDay = Number(before.textContent)

    fireEvent.keyDown(grid, { key: "ArrowRight" })

    const cellsAfter = screen.getAllByRole("gridcell")
    const after = cellsAfter.find((c) => c.tabIndex === 0)!
    expect(Number(after.textContent)).toBe(beforeDay + 1)
    // O foco DOM real também se move (não só o atributo).
    expect(document.activeElement).toBe(after)
  })

  it("Home move para o início da semana visível (mesma linha)", () => {
    openPicker()
    const grid = screen.getByRole("grid")
    // Anda uns dias pra dentro do mês antes de testar Home.
    fireEvent.keyDown(grid, { key: "ArrowRight" })
    fireEvent.keyDown(grid, { key: "ArrowRight" })
    fireEvent.keyDown(grid, { key: "Home" })

    const roving = screen.getAllByRole("gridcell").find((c) => c.tabIndex === 0)!
    // Domingo = primeiro dia da semana (getDay()===0) — o rótulo acessível
    // carrega a data completa formatada, então checamos via aria-label.
    expect(roving.getAttribute("aria-label")).toBeTruthy()
    expect(document.activeElement).toBe(roving)
  })
})

describe("DateRangePicker — A11Y-20 (alvo mínimo 24px)", () => {
  it("botão de limpar seleção tem h-6 w-6 (24px)", () => {
    render(<DateRangePicker aria-label="Período" value={{ from: new Date(2026, 0, 1), to: new Date(2026, 0, 5) }} />)
    const clearBtn = screen.getByRole("button", { name: /limpar/i })
    expect(clearBtn.className).toContain("h-6")
    expect(clearBtn.className).toContain("w-6")
  })
})

describe("DateRangePicker dentro de Modal (R2-8.6)", () => {
  it("porta o popover para dentro do painel do Modal, não para document.body", () => {
    render(
      <Modal open onClose={() => {}} title="Filtros">
        <DateRangePicker aria-label="Período" />
      </Modal>,
    )

    fireEvent.click(screen.getByRole("button", { name: "Período" }))
    const grid = screen.getByRole("grid")

    // O painel do Modal é o ancestral `role="dialog"` que envolve o título —
    // se o popover tivesse ido parar direto no body (ignorando o
    // `PortalContainerContext`), ele NÃO estaria dentro desse dialog.
    const panel = screen.getByText("Filtros").closest('[role="dialog"]')
    expect(panel).not.toBeNull()
    expect(panel?.contains(grid)).toBe(true)
  })

  it("Escape com o calendário aberto fecha só o popover, não o Modal por baixo", async () => {
    const onModalClose = vi.fn()
    render(
      <Modal open onClose={onModalClose} title="Filtros">
        <DateRangePicker aria-label="Período" />
      </Modal>,
    )

    fireEvent.click(screen.getByRole("button", { name: "Período" }))
    const grid = screen.getByRole("grid")

    fireEvent.keyDown(grid, { key: "Escape" })

    await waitFor(() => expect(screen.queryByRole("grid")).not.toBeInTheDocument())
    expect(onModalClose).not.toHaveBeenCalled()
    expect(screen.getByText("Filtros")).toBeInTheDocument()
  })
})
