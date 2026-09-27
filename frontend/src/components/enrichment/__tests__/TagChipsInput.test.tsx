/**
 * TagChipsInput — A11Y-20 (alvo mínimo do botão de remover) e A11Y-42
 * (foco visível no contêiner quando o input interno está focado).
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { TagChipsInput } from "@/components/enrichment/TagChipsInput"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

describe("TagChipsInput", () => {
  it("renderiza as tags existentes como chips", () => {
    render(<TagChipsInput value={["asset_known"]} onChange={vi.fn()} />)
    expect(screen.getByText("asset_known")).toBeInTheDocument()
  })

  it("Enter cria uma nova tag normalizada", () => {
    const onChange = vi.fn()
    render(<TagChipsInput value={[]} onChange={onChange} />)
    // O <input list="..."> (datalist) tem role implícito "combobox", não "textbox".
    const input = screen.getByRole("combobox")
    fireEvent.change(input, { target: { value: "Asset Known" } })
    fireEvent.keyDown(input, { key: "Enter" })
    expect(onChange).toHaveBeenCalledWith(["asset_known"])
  })

  it("botão de remover chama onChange sem a tag", () => {
    const onChange = vi.fn()
    render(<TagChipsInput value={["a", "b"]} onChange={onChange} />)
    fireEvent.click(screen.getByRole("button", { name: "Remover a tag a" }))
    expect(onChange).toHaveBeenCalledWith(["b"])
  })

  // A11Y-20: alvo do botão de remover tag.
  it("botão de remover tem alvo mínimo de 24px (h-6 w-6)", () => {
    render(<TagChipsInput value={["a"]} onChange={vi.fn()} />)
    const removeBtn = screen.getByRole("button", { name: "Remover a tag a" })
    expect(removeBtn.className).toContain("h-6")
    expect(removeBtn.className).toContain("w-6")
  })

  // A11Y-42: o contêiner ganha outline visível quando o input interno tem
  // :focus-visible (mesmo padrão do Checkbox — outline no pai via `:has()`).
  it("contêiner declara outline visível via :has(:focus-visible) (A11Y-42)", () => {
    render(<TagChipsInput value={[]} onChange={vi.fn()} />)
    const input = screen.getByRole("combobox")
    const container = input.parentElement!
    expect(container.className).toContain("outline-primary-500")
    // Regressão: `cn()` (tailwind-merge) descarta a utility `outline` pura
    // quando `outline-2` também está na lista (mesmo grupo de conflito) —
    // sem outline-style setado, o anel nem aparece. `[outline-style:solid]`
    // sobrevive ao merge.
    expect(container.className).toContain("[outline-style:solid]")
  })
})
