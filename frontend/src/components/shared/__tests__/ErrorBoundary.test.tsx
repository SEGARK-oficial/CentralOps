/**
 * Testes — ErrorBoundary.
 * R2-6.5: textos i18n (não mais PT fixo) + "Tentar novamente" recarrega a
 * página de verdade quando o erro é de import() de chunk (React.lazy cacheia
 * a promise rejeitada — resetar o estado local bateria na mesma promise e
 * falharia nas mesmas, em loop).
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { ErrorBoundary } from "@/components/shared/ErrorBoundary"

function Boom({ message }: { message: string }): never {
  throw new Error(message)
}

// A própria classe já loga via console.error (componentDidCatch) — silencia
// pra não poluir a saída do teste (React também loga o erro capturado).
beforeEach(() => {
  vi.spyOn(console, "error").mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe("ErrorBoundary — R2-6.5 (i18n)", () => {
  it("mostra título e descrição traduzidos (pt), não mais fixos", () => {
    render(
      <ErrorBoundary>
        <Boom message="qualquer erro" />
      </ErrorBoundary>,
    )
    expect(screen.getByText("Algo deu errado")).toBeInTheDocument()
    expect(
      screen.getByText(/Encontramos um erro inesperado ao renderizar esta área/),
    ).toBeInTheDocument()
  })

  it("role=alert no container do fallback", () => {
    render(
      <ErrorBoundary>
        <Boom message="qualquer erro" />
      </ErrorBoundary>,
    )
    expect(screen.getByRole("alert")).toBeInTheDocument()
  })
})

describe("ErrorBoundary — R2-6.5 (erro de chunk recarrega a página)", () => {
  // jsdom expõe `window.location.reload` como read-only — `defineProperty`
  // é a forma de sobrescrever pra espionar a chamada.
  function stubReload(): ReturnType<typeof vi.fn> {
    const reloadSpy = vi.fn()
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...window.location, reload: reloadSpy },
    })
    return reloadSpy
  }

  it("erro comum (não é de chunk): 'Tentar novamente' só reseta o estado local (não recarrega)", () => {
    const reloadSpy = stubReload()

    render(
      <ErrorBoundary>
        <Boom message="TypeError: cannot read property x of undefined" />
      </ErrorBoundary>,
    )

    fireEvent.click(screen.getByRole("button", { name: "Tentar novamente" }))
    expect(reloadSpy).not.toHaveBeenCalled()
  })

  it("erro de chunk (import dinâmico falhou): botão primário vira 'Recarregar página' e chama reload", () => {
    const reloadSpy = stubReload()

    render(
      <ErrorBoundary>
        <Boom message="Failed to fetch dynamically imported module: https://app/assets/RoutesPage-abc123.js" />
      </ErrorBoundary>,
    )

    // Mensagem específica de chunk (não a genérica).
    expect(
      screen.getByText(/Uma nova versão do CentralOps foi publicada/),
    ).toBeInTheDocument()

    const button = screen.getByRole("button", { name: "Recarregar página" })
    fireEvent.click(button)
    expect(reloadSpy).toHaveBeenCalledTimes(1)

    // Só um botão aparece nesse modo (o secundário "recarregar" seria redundante).
    expect(screen.getAllByRole("button")).toHaveLength(1)
  })

  it("outra variação de mensagem de chunk (webpack-like 'loading chunk') também é detectada", () => {
    render(
      <ErrorBoundary>
        <Boom message="Loading chunk 42 failed." />
      </ErrorBoundary>,
    )
    expect(screen.getByText(/Uma nova versão do CentralOps foi publicada/)).toBeInTheDocument()
  })

  it("mensagem de erro comum não aciona o texto/fluxo de chunk", () => {
    render(
      <ErrorBoundary>
        <Boom message="Cannot read properties of null" />
      </ErrorBoundary>,
    )
    expect(screen.queryByText(/Uma nova versão do CentralOps foi publicada/)).not.toBeInTheDocument()
    // Os DOIS botões aparecem no modo normal.
    expect(screen.getAllByRole("button")).toHaveLength(2)
  })
})
