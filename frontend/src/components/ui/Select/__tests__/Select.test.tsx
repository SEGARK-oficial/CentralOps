import { describe, it, expect, vi } from "vitest"
import { render, screen, fireEvent, within } from "@testing-library/react"
import { Select } from "../Select"

const OPTIONS = [
  { value: "drop_newest", label: "drop_newest" },
  { value: "drop_oldest", label: "drop_oldest" },
  { value: "block", label: "block" },
]

describe("Select", () => {
  it("abre a lista de opções ao clicar no trigger", () => {
    render(<Select options={OPTIONS} aria-label="backpressure" />)
    expect(screen.queryByRole("listbox")).toBeNull()
    fireEvent.click(screen.getByLabelText("backpressure"))
    const listbox = screen.getByRole("listbox")
    expect(within(listbox).getAllByRole("option")).toHaveLength(3)
  })

  it("emite o valor selecionado", () => {
    const onChange = vi.fn()
    render(<Select options={OPTIONS} aria-label="tier" onChange={onChange} />)
    fireEvent.click(screen.getByLabelText("tier"))
    fireEvent.click(screen.getByRole("option", { name: "block" }))
    expect(onChange).toHaveBeenCalledWith("block")
  })

  it("o dropdown abre na CAMADA popover (acima do modal) — regressão do select atrás do modal", () => {
    // Bug: --z-index-dropdown (1000) < --z-index-modal (1050) ⇒ a lista abria
    // ATRÁS do Modal. Fix: o portal usa var(--z-index-popover) (1060 > 1050).
    render(<Select options={OPTIONS} aria-label="destino" />)
    fireEvent.click(screen.getByLabelText("destino"))
    const portal = screen.getByRole("listbox").closest(".animate-slide-down") as HTMLElement
    expect(portal).not.toBeNull()
    expect(portal.style.zIndex).toBe("var(--z-index-popover)")
  })
})
