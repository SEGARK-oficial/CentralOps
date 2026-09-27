/**
 * R3-8.1 (regressão da Rodada 2) — mesmo problema do Modal: um componente de
 * outro namespace de i18n dentro do Drawer suspendia até o único `<Suspense>`
 * da rota. Sem `<Suspense>` local aqui, este teste falha de verdade (nenhum
 * limite de Suspense na árvore ⇒ React lança em vez de mostrar fallback).
 */
import { render, screen } from "@testing-library/react"
import { Drawer } from "@/components/ui/Drawer/Drawer"

function createSuspender() {
  let resolved = false
  let resolveFn: () => void = () => {}
  const promise = new Promise<void>((res) => {
    resolveFn = () => {
      resolved = true
      res()
    }
  })
  function LateNamespaceContent() {
    if (!resolved) throw promise
    return <p>Conteúdo do outro namespace</p>
  }
  return { LateNamespaceContent, resolve: resolveFn }
}

describe("Drawer — rede de segurança do Suspense (R3-8.1)", () => {
  it("componente suspenso mostra o fallback DENTRO do Drawer, sem sumir com a página", async () => {
    const { LateNamespaceContent, resolve } = createSuspender()

    render(
      <div>
        <p>Página por trás do Drawer</p>
        <Drawer open onClose={() => {}} ariaLabel="Painel">
          <LateNamespaceContent />
        </Drawer>
      </div>,
    )

    expect(screen.getByText("Página por trás do Drawer")).toBeInTheDocument()
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("status")).toBeInTheDocument()
    expect(screen.queryByText("Conteúdo do outro namespace")).not.toBeInTheDocument()

    resolve()
    expect(await screen.findByText("Conteúdo do outro namespace")).toBeInTheDocument()
    expect(screen.getByText("Página por trás do Drawer")).toBeInTheDocument()
  })

  it("o foco continua DENTRO do Drawer (não vaza pro body) enquanto o painel suspende", async () => {
    const { LateNamespaceContent, resolve } = createSuspender()

    render(
      <Drawer open onClose={() => {}} ariaLabel="Painel">
        <LateNamespaceContent />
      </Drawer>,
    )

    // Sem nenhum elemento focável (fallback é só o LoadingSpinner), o Radix
    // FocusScope foca o próprio nó que ele renderiza (o wrapper `tabIndex=-1`
    // que ENVOLVE o painel `role=dialog` — não o painel em si). Por isso o
    // teste de contenção olha as DUAS direções: dentro do diálogo, ou um
    // ancestral do diálogo dentro do próprio trap.
    const isWithinTrap = () => {
      const dialog = screen.getByRole("dialog")
      const active = document.activeElement
      return active === dialog || dialog.contains(active) || (active !== null && active.contains(dialog))
    }
    expect(document.activeElement).not.toBe(document.body)
    expect(isWithinTrap()).toBe(true)

    resolve()
    await screen.findByText("Conteúdo do outro namespace")

    expect(document.activeElement).not.toBe(document.body)
    expect(isWithinTrap()).toBe(true)
  })
})
