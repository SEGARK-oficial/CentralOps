/**
 * Testes de ErrorState — render padrão, variantes, onRetry, acessibilidade.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { ErrorState } from "@/components/ui/ErrorState"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// ── Render padrão ─────────────────────────────────────────────────────────────

describe("ErrorState — render padrão", () => {
  it("renderiza título obrigatório", () => {
    render(<ErrorState title="Falha ao carregar" />)
    expect(screen.getByRole("heading", { name: /falha ao carregar/i })).toBeInTheDocument()
  })

  it("não renderiza mensagem quando não fornecida", () => {
    render(<ErrorState title="Erro" />)
    // Apenas o h3 deve existir; nenhum parágrafo de mensagem
    expect(screen.queryByRole("paragraph")).not.toBeInTheDocument()
  })

  it("renderiza mensagem quando fornecida", () => {
    render(<ErrorState title="Erro" message="Tente novamente mais tarde." />)
    expect(screen.getByText("Tente novamente mais tarde.")).toBeInTheDocument()
  })

  it("não renderiza botão de retry quando onRetry está ausente", () => {
    render(<ErrorState title="Erro" />)
    expect(screen.queryByRole("button", { name: /tentar novamente/i })).not.toBeInTheDocument()
  })
})

// ── onRetry ───────────────────────────────────────────────────────────────────

describe("ErrorState — onRetry", () => {
  it("renderiza botão 'Tentar novamente' quando onRetry fornecido", () => {
    render(<ErrorState title="Erro" onRetry={() => {}} />)
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
  })

  it("chama onRetry ao clicar no botão", () => {
    const onRetry = vi.fn()
    render(<ErrorState title="Erro" onRetry={onRetry} />)
    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it("dispara onRetry a cada clique adicional", () => {
    const onRetry = vi.fn()
    render(<ErrorState title="Erro" onRetry={onRetry} />)
    const btn = screen.getByRole("button", { name: /tentar novamente/i })
    fireEvent.click(btn)
    fireEvent.click(btn)
    fireEvent.click(btn)
    expect(onRetry).toHaveBeenCalledTimes(3)
  })
})

// ── Variantes ─────────────────────────────────────────────────────────────────

describe("ErrorState — variante inline (padrão)", () => {
  it("não tem min-h-screen (não é full-page)", () => {
    render(<ErrorState title="Erro" />)
    const alert = screen.getByRole("alert")
    expect(alert.className).not.toContain("min-h-screen")
  })
})

describe("ErrorState — variante full-page", () => {
  it("tem min-h-screen", () => {
    render(<ErrorState title="Erro" variant="full-page" />)
    const alert = screen.getByRole("alert")
    expect(alert.className).toContain("min-h-screen")
  })

  it("renderiza título, mensagem e botão de retry em full-page", () => {
    const onRetry = vi.fn()
    render(
      <ErrorState
        title="Serviço indisponível"
        message="O servidor não está respondendo."
        onRetry={onRetry}
        variant="full-page"
      />,
    )
    expect(screen.getByRole("heading", { name: /serviço indisponível/i })).toBeInTheDocument()
    expect(screen.getByText(/o servidor não está respondendo/i)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /tentar novamente/i })).toBeInTheDocument()
  })
})

// ── Acessibilidade ────────────────────────────────────────────────────────────

describe("ErrorState — acessibilidade", () => {
  it("container tem role=alert para anúncio imediato", () => {
    render(<ErrorState title="Erro grave" />)
    expect(screen.getByRole("alert")).toBeInTheDocument()
  })

  it("container tem aria-live=assertive", () => {
    render(<ErrorState title="Erro grave" />)
    expect(screen.getByRole("alert")).toHaveAttribute("aria-live", "assertive")
  })

  it("botão de retry é focalizável via teclado (não desabilitado)", () => {
    render(<ErrorState title="Erro" onRetry={() => {}} />)
    const btn = screen.getByRole("button", { name: /tentar novamente/i })
    expect(btn).not.toBeDisabled()
  })

  it("aceita className extra sem sobrescrever role=alert", () => {
    render(<ErrorState title="Erro" className="mt-8" />)
    const alert = screen.getByRole("alert")
    expect(alert.className).toContain("mt-8")
    expect(alert).toHaveAttribute("role", "alert")
  })
})
