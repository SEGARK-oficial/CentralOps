import { render, screen } from "@testing-library/react"
import { JsonViewer } from "@/components/shared/JsonViewer"

describe("JsonViewer", () => {
  it("renderiza objeto plano com chave visível", () => {
    render(<JsonViewer data={{ name: "wazuh", version: 1 }} />)
    expect(screen.getByText(/name/)).toBeInTheDocument()
  })

  it("renderiza objeto aninhado sem crashar", () => {
    const nested = { outer: { inner: { deep: "valor" } } }
    const { container } = render(<JsonViewer data={nested} />)
    // Antes este teste procurava um elemento com classe contendo "json" — e passava
    // por acidente: quem casava era o próprio seletor Tailwind `[&_.json-view-lite]`
    // do wrapper, que mirava classes que esta lib NÃO emite (ela usa CSS modules com
    // hash). Ou seja, a asserção provava a existência justamente do override que não
    // funcionava. Agora verifica o que importa: a chave de topo foi renderizada.
    expect(container.textContent).toContain("outer")
  })

  it("não crasha com null", () => {
    expect(() => render(<JsonViewer data={null} />)).not.toThrow()
  })

  it("não crasha com undefined", () => {
    expect(() => render(<JsonViewer data={undefined} />)).not.toThrow()
  })

  it("aceita collapseLevel personalizado", () => {
    expect(() => render(<JsonViewer data={{ a: 1 }} collapseLevel={0} />)).not.toThrow()
  })
})

// ── LAY-07: tema não pode vir do preset Solarized da lib ────────────────────
describe("JsonViewer — LAY-07 (tokens do design system, não Solarized)", () => {
  it("usa tokens de texto do DS (text-text/text-text-secondary/text-text-tertiary), não classes hash da lib", () => {
    const { container } = render(<JsonViewer data={{ key: "value", n: 1 }} collapseLevel={Infinity} />)
    const html = container.innerHTML
    // As classes hash que a lib usa pro preset Solarized (ver dist/index.css)
    // não devem aparecer — confirma que NÃO estamos mais usando darkStyles/defaultStyles.
    expect(html).not.toMatch(/_11RoI|_Chy1W|_2bveF|_2vRm-|_1prJR/)
  })

  it("container não pinta fundo opaco (bg-transparent) — quem dá o fundo é o call-site", () => {
    const { container } = render(<JsonViewer data={{ a: 1 }} />)
    const jsonRoot = container.querySelector('[role="tree"]')
    expect(jsonRoot).toHaveClass("bg-transparent")
  })

  it("valores usam font-mono (convenção do DS para dado/código)", () => {
    render(<JsonViewer data={{ name: "wazuh" }} collapseLevel={Infinity} />)
    const value = screen.getByText('"wazuh"')
    expect(value.className).toMatch(/font-mono/)
  })
})
