/**
 * Testes de render — RouteConditionEditor.
 * R2-6.1: da 2ª linha em diante, os controles ficavam sem nome acessível
 * (só a 1ª linha tinha `label` visível, e o `aria-label` do operador era
 * estático — duas linhas anunciavam o MESMO nome "Operador da condição").
 */
import { useState } from "react"
import { render, screen } from "@testing-library/react"
import { RouteConditionEditor } from "@/components/routes/RouteConditionEditor"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function Wrapper({ initial }: { initial: Array<{ field: string; op: string; value: string }> }) {
  const [clauses, setClauses] = useState(initial as never)
  return <RouteConditionEditor clauses={clauses} onChange={setClauses as never} />
}

describe("RouteConditionEditor — nome acessível por linha (R2-6.1)", () => {
  const TWO_CLAUSES = [
    { field: "severity_id", op: "gte", value: "4" },
    { field: "vendor", op: "eq", value: "sophos" },
  ]

  it("cada select de campo tem aria-label indexado, mesmo sem label visível", () => {
    render(<Wrapper initial={TWO_CLAUSES} />)
    expect(screen.getByRole("button", { name: /campo da condição 1/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /campo da condição 2/i })).toBeInTheDocument()
  })

  it("cada select de operador tem aria-label indexado (não repete o mesmo nome)", () => {
    render(<Wrapper initial={TWO_CLAUSES} />)
    const op1 = screen.getByRole("button", { name: /operador da condição 1/i })
    const op2 = screen.getByRole("button", { name: /operador da condição 2/i })
    expect(op1).toBeInTheDocument()
    expect(op2).toBeInTheDocument()
    expect(op1).not.toBe(op2)
  })

  it("cada input de valor tem aria-label indexado", () => {
    render(<Wrapper initial={TWO_CLAUSES} />)
    expect(screen.getByRole("textbox", { name: /valor da condição 1/i })).toBeInTheDocument()
    expect(screen.getByRole("textbox", { name: /valor da condição 2/i })).toBeInTheDocument()
  })

  it("select de valor (op=exists) também tem aria-label indexado", () => {
    render(
      <Wrapper
        initial={[
          { field: "detection_matched", op: "exists", value: "true" },
          { field: "vendor", op: "exists", value: "false" },
        ]}
      />,
    )
    expect(screen.getByRole("button", { name: /valor da condição 1/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /valor da condição 2/i })).toBeInTheDocument()
  })

  it("botão de remover tem aria-label indexado por linha", () => {
    render(<Wrapper initial={TWO_CLAUSES} />)
    expect(screen.getByRole("button", { name: /remover condição 1/i })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /remover condição 2/i })).toBeInTheDocument()
  })

  it("a 1ª linha continua com label visível (não regride)", () => {
    render(<Wrapper initial={TWO_CLAUSES} />)
    expect(screen.getByText("Campo")).toBeInTheDocument()
    expect(screen.getByText("Op")).toBeInTheDocument()
    expect(screen.getByText("Valor")).toBeInTheDocument()
  })

  it("3 linhas: nenhum par de aria-label se repete", () => {
    render(
      <Wrapper
        initial={[
          { field: "severity_id", op: "gte", value: "1" },
          { field: "vendor", op: "eq", value: "a" },
          { field: "platform", op: "ne", value: "b" },
        ]}
      />,
    )
    const buttons = screen.getAllByRole("button").map((b) => b.getAttribute("aria-label")).filter(Boolean)
    expect(new Set(buttons).size).toBe(buttons.length)
  })
})
