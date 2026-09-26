/**
 * Testes — MappingEditorLayout.
 * R2-6.7: o botão de colapsar/expandir o painel de payload vivia DENTRO do
 * `PanelResizeHandle` (role=separator) — separator com `<button>` aninhado é
 * inválido (ARIA) e o clique disputava o gesto de arrastar com o resize. O
 * botão virou IRMÃO do handle; o handle cresceu de 12px pra 24px (WCAG 2.5.8).
 */
import { render, screen } from "@testing-library/react"
import { MappingEditorLayout } from "@/components/mappings/MappingEditorLayout"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

/** Força `useMediaQuery` a resolver "é desktop largo" (>=1280px) — o mock
 *  global de matchMedia (test/setup.ts) sempre devolve `matches:false`. */
function mockWideViewport(): () => void {
  const original = window.matchMedia
  window.matchMedia = ((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
  return () => {
    window.matchMedia = original
  }
}

describe("MappingEditorLayout — desktop largo (painéis redimensionáveis)", () => {
  let restoreViewport: () => void

  beforeEach(() => {
    restoreViewport = mockWideViewport()
  })

  afterEach(() => {
    restoreViewport()
  })

  it("renderiza os 3 painéis", () => {
    render(
      <MappingEditorLayout
        payload={<div>Payload aqui</div>}
        rules={<div>Regras aqui</div>}
        envelope={<div>Envelope aqui</div>}
      />,
    )
    expect(screen.getByText("Payload aqui")).toBeInTheDocument()
    expect(screen.getByText("Regras aqui")).toBeInTheDocument()
    expect(screen.getByText("Envelope aqui")).toBeInTheDocument()
  })

  it("o botão de colapsar o payload NÃO é descendente do separator (R2-6.7)", () => {
    render(
      <MappingEditorLayout
        payload={<div>Payload aqui</div>}
        rules={<div>Regras aqui</div>}
        envelope={<div>Envelope aqui</div>}
      />,
    )
    const button = screen.getByRole("button", { name: "Recolher painel de amostra" })
    const separators = screen.getAllByRole("separator")
    for (const sep of separators) {
      expect(sep.contains(button)).toBe(false)
    }
  })

  it("existem 2 separators (payload│regras e regras│envelope), cada um sem botão dentro", () => {
    render(
      <MappingEditorLayout
        payload={<div>Payload aqui</div>}
        rules={<div>Regras aqui</div>}
        envelope={<div>Envelope aqui</div>}
      />,
    )
    const separators = screen.getAllByRole("separator")
    expect(separators).toHaveLength(2)
    for (const sep of separators) {
      expect(sep.querySelector("button")).toBeNull()
    }
  })

  it("botão de colapsar tem o aria-label inicial (estado expandido)", () => {
    render(
      <MappingEditorLayout
        payload={<div>Payload aqui</div>}
        rules={<div>Regras aqui</div>}
        envelope={<div>Envelope aqui</div>}
      />,
    )
    // O clique de verdade aciona `panel.isCollapsed()`, que exige layout real
    // (largura calculada) que o jsdom não fornece — cobrimos aqui só o
    // contrato estrutural/a11y (posição do botão), não o gesto de resize.
    expect(screen.getByRole("button", { name: "Recolher painel de amostra" })).toBeInTheDocument()
  })
})

describe("MappingEditorLayout — empilhado (<1280px)", () => {
  it("sem separators/resize — só as 3 seções empilhadas", () => {
    render(
      <MappingEditorLayout
        payload={<div>Payload aqui</div>}
        rules={<div>Regras aqui</div>}
        envelope={<div>Envelope aqui</div>}
      />,
    )
    expect(screen.queryAllByRole("separator")).toHaveLength(0)
    expect(screen.getByText("Payload aqui")).toBeInTheDocument()
  })
})
