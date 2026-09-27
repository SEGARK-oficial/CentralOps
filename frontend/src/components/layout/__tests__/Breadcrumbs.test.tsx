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

describe("Breadcrumbs — segmento intermediário só é link se for página", () => {
  const POLICY = "3f2b8c1e-9a4d-4e6f-8b2a-1c5d7e9f0a3b"

  it("no editor de política, 'Políticas' leva à aba de políticas, que existe", () => {
    renderAt(`/enrichment/policies/${POLICY}`)
    expect(screen.getByRole("link", { name: /Enriquecimento/ })).toHaveAttribute("href", "/enrichment")
    expect(screen.getByRole("link", { name: /Políticas/ })).toHaveAttribute("href", "/enrichment/policies")
  })

  it("UUID no fim vira 'Detalhe', não o id cru", () => {
    renderAt(`/enrichment/policies/${POLICY}`)
    expect(screen.getByText("Detalhe")).toBeInTheDocument()
    expect(screen.queryByText(POLICY)).not.toBeInTheDocument()
  })

  it("caminho intermediário sem página conhecida não vira link", () => {
    // Sem rótulo = sem rota registrada: oferecer o link seria oferecer um 404.
    renderAt("/rota-desconhecida/filho/123")
    expect(screen.queryByRole("link", { name: /rota-desconhecida/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("link", { name: /filho/ })).not.toBeInTheDocument()
    expect(screen.getByText("rota-desconhecida")).toBeInTheDocument()
  })
})
