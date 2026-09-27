/**
 * Testes — JMESPathInput (autocomplete de campos JMESPath).
 * LAY-39: a lista de sugestões é portada pro <body> (position:fixed), então
 * não é mais um descendente DOM do wrapper do input.
 */
import { useState } from "react"
import { render, screen, fireEvent } from "@testing-library/react"
import { JMESPathInput } from "@/components/mappings/JMESPathInput"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const SUGGESTIONS = ["data.alert.severity", "data.alert.action", "data.user.name"]

describe("JMESPathInput", () => {
  it("não mostra dropdown quando o input está vazio e sem foco", () => {
    render(<JMESPathInput value="" onChange={vi.fn()} suggestions={SUGGESTIONS} />)
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument()
  })

  it("mostra sugestões filtradas ao focar/digitar", () => {
    render(<JMESPathInput value="data.alert" onChange={vi.fn()} suggestions={SUGGESTIONS} />)
    fireEvent.focus(screen.getByRole("textbox"))
    expect(screen.getByRole("listbox")).toBeInTheDocument()
    expect(screen.getByText("data.alert.severity")).toBeInTheDocument()
    expect(screen.getByText("data.alert.action")).toBeInTheDocument()
    expect(screen.queryByText("data.user.name")).not.toBeInTheDocument()
  })

  it("LAY-39: a lista é renderizada via portal, fora do wrapper do input", () => {
    const { container } = render(
      <JMESPathInput value="data" onChange={vi.fn()} suggestions={SUGGESTIONS} />,
    )
    fireEvent.focus(screen.getByRole("textbox"))
    const listbox = screen.getByRole("listbox")
    // Não é descendente do wrapper renderizado pelo componente…
    expect(container.contains(listbox)).toBe(false)
    // …mas está no documento (portado pro body).
    expect(document.body.contains(listbox)).toBe(true)
  })

  it("LAY-39: a lista usa position:fixed com z-index de popover (não presa ao overflow do painel)", () => {
    render(<JMESPathInput value="data" onChange={vi.fn()} suggestions={SUGGESTIONS} />)
    fireEvent.focus(screen.getByRole("textbox"))
    const listbox = screen.getByRole("listbox")
    expect(listbox.style.position).toBe("fixed")
    expect(listbox.style.zIndex).toBe("var(--z-index-popover)")
  })

  it("ArrowDown navega pelas opções e Enter seleciona", () => {
    const onChange = vi.fn()
    render(<JMESPathInput value="data.alert" onChange={onChange} suggestions={SUGGESTIONS} />)
    const input = screen.getByRole("textbox")
    fireEvent.focus(input)
    fireEvent.keyDown(input, { key: "ArrowDown" })
    fireEvent.keyDown(input, { key: "Enter" })
    expect(onChange).toHaveBeenCalledWith("data.alert.severity")
  })

  it("clicar numa sugestão seleciona e fecha o dropdown", () => {
    // Wrapper controlado: só com o `value` realmente atualizando, `filtered`
    // exclui a sugestão escolhida (`s !== value`) e o dropdown fecha de fato
    // mesmo com o refoco do input após a seleção.
    function Wrapper() {
      const [value, setValue] = useState("data.alert")
      return <JMESPathInput value={value} onChange={setValue} suggestions={SUGGESTIONS} />
    }
    render(<Wrapper />)
    fireEvent.focus(screen.getByRole("textbox"))
    fireEvent.mouseDown(screen.getByText("data.alert.action"))
    expect(screen.getByRole("textbox")).toHaveValue("data.alert.action")
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument()
  })

  it("Escape fecha o dropdown sem selecionar", () => {
    const onChange = vi.fn()
    render(<JMESPathInput value="data.alert" onChange={onChange} suggestions={SUGGESTIONS} />)
    const input = screen.getByRole("textbox")
    fireEvent.focus(input)
    expect(screen.getByRole("listbox")).toBeInTheDocument()
    fireEvent.keyDown(input, { key: "Escape" })
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
  })

  it("clique fora fecha o dropdown (considerando a lista portada)", () => {
    render(
      <div>
        <JMESPathInput value="data.alert" onChange={vi.fn()} suggestions={SUGGESTIONS} />
        <button type="button">fora</button>
      </div>,
    )
    fireEvent.focus(screen.getByRole("textbox"))
    expect(screen.getByRole("listbox")).toBeInTheDocument()
    fireEvent.mouseDown(screen.getByRole("button", { name: "fora" }))
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument()
  })

  it("sem sugestões: comporta-se como input de texto livre, sem dropdown", () => {
    render(<JMESPathInput value="qualquer.coisa" onChange={vi.fn()} suggestions={[]} />)
    fireEvent.focus(screen.getByRole("textbox"))
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument()
  })
})
