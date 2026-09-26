/**
 * R3-8.1 (regressão da Rodada 2) — com namespaces de i18n sob demanda
 * (`useSuspense: true`), um componente de outro namespace montado DENTRO de
 * um Modal suspendia até o único `<Suspense>` da rota (`AppLayout`): a PÁGINA
 * inteira sumia (não só o Modal) e o foco caía no `body`.
 *
 * Sem `<Suspense>` local no Modal, este teste falha de verdade: React não
 * acha nenhum limite de Suspense na árvore (o teste renderiza só o Modal, sem
 * o `AppLayout` por trás) e lança em vez de mostrar um fallback contido.
 */
import { render, screen } from "@testing-library/react"
import { Modal } from "@/components/ui/Modal/Modal"

/** Suspende (lança a promise) até `resolve()` ser chamado de fora do teste. */
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
    return <button type="button">Conteúdo do outro namespace</button>
  }
  return { LateNamespaceContent, resolve: resolveFn }
}

describe("Modal — rede de segurança do Suspense (R3-8.1)", () => {
  it("componente de outro namespace suspenso mostra o fallback DENTRO do Modal, sem sumir com a página", async () => {
    const { LateNamespaceContent, resolve } = createSuspender()

    render(
      <div>
        <p>Página por trás do Modal</p>
        <Modal open onClose={() => {}} title="Config">
          <LateNamespaceContent />
        </Modal>
      </div>,
    )

    // A página NÃO desaparece — só o painel mostra o fallback compacto.
    expect(screen.getByText("Página por trás do Modal")).toBeInTheDocument()
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByRole("status")).toBeInTheDocument() // fallback do LoadingSpinner
    expect(screen.queryByText("Conteúdo do outro namespace")).not.toBeInTheDocument()

    resolve()
    expect(await screen.findByText("Conteúdo do outro namespace")).toBeInTheDocument()
    // A página e o Modal continuaram no ar o tempo todo.
    expect(screen.getByText("Página por trás do Modal")).toBeInTheDocument()
  })

  it("o foco continua DENTRO do Modal quando o painel suspende e volta", async () => {
    const { LateNamespaceContent, resolve } = createSuspender()

    render(
      <Modal open onClose={() => {}} title="Config">
        <LateNamespaceContent />
      </Modal>,
    )

    // O único elemento focável fora do conteúdo suspenso é o botão "Fechar
    // modal" do header — é nele que o FocusScope trapped assentado.
    const closeButton = screen.getByRole("button", { name: /fechar modal/i })
    expect(document.activeElement).toBe(closeButton)

    resolve()
    await screen.findByText("Conteúdo do outro namespace")

    // O foco não vazou pro body enquanto o painel suspendia/resolvia.
    expect(document.activeElement).not.toBe(document.body)
    expect(screen.getByRole("dialog").contains(document.activeElement)).toBe(true)
  })
})
