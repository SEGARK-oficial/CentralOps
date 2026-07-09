import { fireEvent, render, screen } from "@testing-library/react"
import { useState } from "react"
import { Checkbox } from "@/components/ui/Checkbox/Checkbox"

describe("Checkbox", () => {
  it("renders unchecked by default", () => {
    render(<Checkbox label="Aprovar tenant" />)
    const input = screen.getByRole("checkbox", { name: /Aprovar tenant/i })
    expect(input).not.toBeChecked()
    expect(input).toHaveAttribute("aria-checked", "false")
  })

  it("renders checked when controlled", () => {
    render(<Checkbox label="Selecionado" checked onChange={() => {}} />)
    const input = screen.getByRole("checkbox", { name: /Selecionado/i })
    expect(input).toBeChecked()
    expect(input).toHaveAttribute("aria-checked", "true")
  })

  it("renders indeterminate state with mixed aria + minus icon", () => {
    render(<Checkbox label="Selecionar todos" indeterminate checked={false} onChange={() => {}} />)
    const input = screen.getByRole("checkbox", { name: /Selecionar todos/i }) as HTMLInputElement
    expect(input.indeterminate).toBe(true)
    expect(input).toHaveAttribute("aria-checked", "mixed")
  })

  it("fires onChange and updates state when controlled", () => {
    const Harness = () => {
      const [v, setV] = useState(false)
      return <Checkbox label="Toggle" checked={v} onChange={(e) => setV(e.target.checked)} />
    }
    render(<Harness />)
    const input = screen.getByRole("checkbox", { name: /Toggle/i })
    expect(input).not.toBeChecked()
    fireEvent.click(input)
    expect(input).toBeChecked()
    fireEvent.click(input)
    expect(input).not.toBeChecked()
  })

  it("respects disabled and prevents user interaction", () => {
    // jsdom synthetic fireEvent.click ainda dispara onChange em disabled,
    // mas browsers reais não. Validamos o atributo + ausencia de interação real
    // (clicar no label associado a um input disabled não toggla state).
    const Harness = () => {
      const [v, setV] = useState(false)
      return <Checkbox label="Bloqueado" disabled checked={v} onChange={(e) => setV(e.target.checked)} />
    }
    render(<Harness />)
    const input = screen.getByRole("checkbox", { name: /Bloqueado/i })
    expect(input).toBeDisabled()
    expect(input).not.toBeChecked()
    // Clique no label não deve toggar (nativamente, label[for] em input disabled é no-op).
    fireEvent.click(screen.getByText(/Bloqueado/))
    expect(input).not.toBeChecked()
  })

  it("associates label via htmlFor and clicking the label toggles input", () => {
    const Harness = () => {
      const [v, setV] = useState(false)
      return <Checkbox label="Click label" checked={v} onChange={(e) => setV(e.target.checked)} />
    }
    render(<Harness />)
    fireEvent.click(screen.getByText(/Click label/))
    expect(screen.getByRole("checkbox", { name: /Click label/ })).toBeChecked()
  })

  it("uses aria-label fallback when no visible label", () => {
    render(<Checkbox aria-label="aprovar linha 5" />)
    expect(screen.getByRole("checkbox", { name: /aprovar linha 5/i })).toBeInTheDocument()
  })

  it("renders description and links via aria-describedby", () => {
    render(<Checkbox label="Auto-approve" description="Aplica em novos tenants apenas" />)
    const input = screen.getByRole("checkbox", { name: /Auto-approve/ })
    const describedBy = input.getAttribute("aria-describedby")
    expect(describedBy).toBeTruthy()
    const desc = document.getElementById(describedBy!.split(" ")[0])
    expect(desc).toHaveTextContent("Aplica em novos tenants apenas")
  })

  it("shows error and sets aria-invalid", () => {
    render(<Checkbox label="X" error="Obrigatório" onChange={() => {}} />)
    const input = screen.getByRole("checkbox", { name: /X/ })
    expect(input).toHaveAttribute("aria-invalid", "true")
    expect(screen.getByRole("alert")).toHaveTextContent("Obrigatório")
  })

  it("supports size sm and md without throwing", () => {
    const { rerender } = render(<Checkbox label="A" size="sm" />)
    expect(screen.getByRole("checkbox", { name: /A/ })).toBeInTheDocument()
    rerender(<Checkbox label="A" size="md" />)
    expect(screen.getByRole("checkbox", { name: /A/ })).toBeInTheDocument()
  })

  it("hideLabel keeps label for screen readers but visually hides it", () => {
    render(<Checkbox label="Hidden text" hideLabel />)
    const label = screen.getByText("Hidden text")
    expect(label.closest("label")).toHaveClass("sr-only")
  })
})
