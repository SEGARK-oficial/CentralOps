import { render, screen } from "@testing-library/react"
import { JsonViewer } from "@/components/shared/JsonViewer"

describe("JsonViewer", () => {
  it("renderiza objeto plano com chave visível", () => {
    render(<JsonViewer data={{ name: "wazuh", version: 1 }} />)
    expect(screen.getByText(/name/)).toBeInTheDocument()
  })

  it("renderiza objeto aninhado sem crashar", () => {
    const nested = { outer: { inner: { deep: "valor" } } }
    render(<JsonViewer data={nested} />)
    // O container deve estar presente mesmo com nós colapsados
    expect(document.querySelector("[class*='json']")).toBeTruthy()
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
