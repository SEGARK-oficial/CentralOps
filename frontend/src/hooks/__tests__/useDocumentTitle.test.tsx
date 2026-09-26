import { useRef } from "react"
import { render } from "@testing-library/react"
import { useDocumentTitle } from "@/hooks/useDocumentTitle"

function Harness({ routeKey, children }: { routeKey: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useDocumentTitle(ref, routeKey)
  return <div ref={ref}>{children}</div>
}

describe("useDocumentTitle", () => {
  afterEach(() => {
    document.title = ""
  })

  it("define document.title a partir do <h1> do container", () => {
    render(
      <Harness routeKey="/dashboard">
        <h1>Dashboard</h1>
      </Harness>,
    )
    expect(document.title).toBe("Dashboard — CentralOps")
  })

  it("usa só 'CentralOps' quando não há <h1>", () => {
    render(
      <Harness routeKey="/empty">
        <p>Sem título</p>
      </Harness>,
    )
    expect(document.title).toBe("CentralOps")
  })

  it("atualiza quando o <h1> muda de texto depois de um fetch assíncrono (MutationObserver)", async () => {
    function AsyncHarness() {
      const ref = useRef<HTMLDivElement>(null)
      useDocumentTitle(ref, "/integrations/1")
      return (
        <div ref={ref}>
          <h1 id="h1-target"></h1>
        </div>
      )
    }
    render(<AsyncHarness />)
    expect(document.title).toBe("CentralOps")

    const h1 = document.getElementById("h1-target")!
    h1.textContent = "Integração Sophos"

    await Promise.resolve()
    await new Promise((r) => setTimeout(r, 0))

    expect(document.title).toBe("Integração Sophos — CentralOps")
  })

  it("re-deriva o título quando routeKey muda e o novo <h1> já está no container", () => {
    const { rerender } = render(
      <Harness routeKey="/a">
        <h1>Página A</h1>
      </Harness>,
    )
    expect(document.title).toBe("Página A — CentralOps")

    rerender(
      <Harness routeKey="/b">
        <h1>Página B</h1>
      </Harness>,
    )
    expect(document.title).toBe("Página B — CentralOps")
  })
})
