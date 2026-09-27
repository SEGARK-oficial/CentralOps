import { render, screen } from "@testing-library/react"
import { Input } from "@/components/ui/Input/Input"

describe("Input — autoComplete (SEC-02/SEC-10/A11Y-21)", () => {
  it("type=password sem autoComplete explícito usa new-password", () => {
    render(<Input type="password" label="Token da API" />)
    expect(screen.getByLabelText("Token da API")).toHaveAttribute("autoComplete", "new-password")
  })

  it("respeita autoComplete explícito (ex.: login usa current-password)", () => {
    render(<Input type="password" label="Senha" autoComplete="current-password" />)
    expect(screen.getByLabelText("Senha")).toHaveAttribute("autoComplete", "current-password")
  })

  it("campo de texto comum não ganha autoComplete por padrão", () => {
    render(<Input type="text" label="Nome" />)
    expect(screen.getByLabelText("Nome")).not.toHaveAttribute("autoComplete")
  })
})
