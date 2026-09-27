/**
 * Testes — BucketSectionComponent (SEC-04: href do backend validado antes de
 * ir pro navegador).
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { BucketSectionComponent } from "@/components/dashboard/BucketSectionComponent"
import type { BucketSection } from "@/types"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function renderSection(section: BucketSection) {
  return render(
    <MemoryRouter>
      <BucketSectionComponent section={section} />
    </MemoryRouter>,
  )
}

const baseSection = (items: BucketSection["items"]): BucketSection => ({
  id: "s1",
  label: "Integrações com problema",
  items,
})

describe("BucketSectionComponent — SEC-04 (href validado)", () => {
  it("item sem href não é clicável (div, não button)", () => {
    renderSection(baseSection([{ id: "i1", label: "Wazuh", value: 3 }]))
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
    expect(screen.getByText("Wazuh")).toBeInTheDocument()
  })

  it("item com path interno seguro é clicável e navega", () => {
    renderSection(baseSection([{ id: "i1", label: "Wazuh", value: 3, href: "/integrations/1" }]))
    expect(screen.getByRole("button", { name: /Wazuh/ })).toBeInTheDocument()
  })

  it("item com URL externa http(s) segura é clicável", () => {
    renderSection(
      baseSection([{ id: "i1", label: "Docs", value: 1, href: "https://docs.example.com/x" }]),
    )
    expect(screen.getByRole("button", { name: /Docs/ })).toBeInTheDocument()
  })

  it("esquema javascript: não vira clicável (item some como link, mas o texto continua visível)", () => {
    renderSection(
      // eslint-disable-next-line no-script-url -- payload de teste: prova que o esquema É rejeitado.
      baseSection([{ id: "i1", label: "Malicioso", value: 1, href: "javascript:alert(1)" }]),
    )
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
    expect(screen.getByText("Malicioso")).toBeInTheDocument()
  })

  it("path interno tipo protocol-relative (//evil.com) não vira clicável", () => {
    renderSection(baseSection([{ id: "i1", label: "Suspeito", value: 1, href: "//evil.com/x" }]))
    expect(screen.queryByRole("button")).not.toBeInTheDocument()
  })

  it("window.open é chamado com o href externo validado, com noopener,noreferrer", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null)
    renderSection(
      baseSection([{ id: "i1", label: "Docs", value: 1, href: "https://docs.example.com/x" }]),
    )
    fireEvent.click(screen.getByRole("button", { name: /Docs/ }))
    expect(openSpy).toHaveBeenCalledWith("https://docs.example.com/x", "_blank", "noopener,noreferrer")
    openSpy.mockRestore()
  })
})
