import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { Breadcrumbs } from "@/components/layout/Breadcrumbs"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Breadcrumbs />
    </MemoryRouter>,
  )
}

describe("Breadcrumbs — LAY-26 (truncate + title em segmento longo)", () => {
  it("dá title com o rótulo completo no item final (não-link)", () => {
    renderAt("/mappings")
    const current = screen.getByText("Mapeamentos")
    expect(current).toHaveAttribute("title", "Mapeamentos")
    expect(current.className).toMatch(/truncate/)
  })

  it("dá title com o rótulo completo no item intermediário (link)", () => {
    renderAt("/mappings/123")
    const link = screen.getByRole("link", { name: /Mapeamentos/ })
    const label = link.querySelector("span[title]")
    expect(label).toHaveAttribute("title", "Mapeamentos")
    expect(label?.className).toMatch(/truncate/)
  })
})
