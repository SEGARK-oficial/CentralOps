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
