import { useState } from "react"
import { render, screen, fireEvent } from "@testing-library/react"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"

function Harness() {
  const [value, setValue] = useState<"a" | "b" | "c">("a")
  return (
    <Tabs value={value} onValueChange={(v) => setValue(v as "a" | "b" | "c")}>
      <TabsList ariaLabel="Seções">
        <TabsTrigger value="a">Email</TabsTrigger>
        <TabsTrigger value="b">Collector</TabsTrigger>
        <TabsTrigger value="c">Outra aba bem comprida para forçar overflow</TabsTrigger>
      </TabsList>
      <TabsPanel value="a">Painel A</TabsPanel>
      <TabsPanel value="b">Painel B</TabsPanel>
      <TabsPanel value="c">Painel C</TabsPanel>
    </Tabs>
  )
}

describe("Tabs", () => {
  it("troca de painel ao clicar na aba", () => {
    render(<Harness />)
    expect(screen.getByText("Painel A")).toBeInTheDocument()
    fireEvent.click(screen.getByRole("tab", { name: "Collector" }))
    expect(screen.getByText("Painel B")).toBeInTheDocument()
  })

  it("ArrowRight move para a próxima aba e ativa", () => {
    render(<Harness />)
    const first = screen.getByRole("tab", { name: "Email" })
    first.focus()
    fireEvent.keyDown(first, { key: "ArrowRight" })
    expect(screen.getByText("Painel B")).toBeInTheDocument()
  })

  // LAY-21: a lista rola horizontalmente (flex-nowrap + overflow-x-auto) em
  // vez de empilhar as abas em várias linhas (flex-wrap).
  it("TabsList não usa flex-wrap — usa flex-nowrap com overflow-x-auto", () => {
    render(<Harness />)
    const tablist = screen.getByRole("tablist")
    expect(tablist.className).toContain("flex-nowrap")
    expect(tablist.className).toContain("overflow-x-auto")
    expect(tablist.className).not.toContain("flex-wrap")
  })

  it("cada TabsTrigger tem shrink-0 (não comprime no flex-nowrap)", () => {
    render(<Harness />)
    for (const tab of screen.getAllByRole("tab")) {
      expect(tab.className).toContain("shrink-0")
    }
  })
})
