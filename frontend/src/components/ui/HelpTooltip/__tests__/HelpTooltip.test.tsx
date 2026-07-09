/**
 * Testes de HelpTooltip
 * Cobre: render, open/close via click, escape, focus (keyboard), example, learnMoreHref.
 */

import { render, screen, fireEvent, act } from "@testing-library/react"
import { HelpTooltip } from "@/components/ui/HelpTooltip/HelpTooltip"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

describe("HelpTooltip", () => {
  it("renderiza o trigger com aria-label correto", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope normalizado." />)

    const trigger = screen.getByRole("button", { name: /ajuda: target/i })
    expect(trigger).toBeInTheDocument()
  })

  it("tooltip não está visível por padrão", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope normalizado." />)

    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument()
  })

  it("abre o tooltip ao clicar no trigger", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope normalizado." />)

    const trigger = screen.getByRole("button", { name: /ajuda: target/i })
    fireEvent.click(trigger)

    expect(screen.getByRole("tooltip")).toBeInTheDocument()
    expect(screen.getByText("Caminho no envelope normalizado.")).toBeInTheDocument()
  })

  it("fecha o tooltip ao clicar novamente no trigger (toggle)", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope normalizado." />)

    const trigger = screen.getByRole("button", { name: /ajuda: target/i })
    fireEvent.click(trigger)
    expect(screen.getByRole("tooltip")).toBeInTheDocument()

    fireEvent.click(trigger)
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument()
  })

  it("fecha o tooltip ao pressionar Escape no trigger", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope normalizado." />)

    const trigger = screen.getByRole("button", { name: /ajuda: target/i })
    fireEvent.click(trigger)
    expect(screen.getByRole("tooltip")).toBeInTheDocument()

    // Escape no trigger (via React synthetic events — compatível com jsdom)
    fireEvent.keyDown(trigger, { key: "Escape" })
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument()
  })

  it("abre o tooltip ao receber foco (keyboard tab)", () => {
    render(<HelpTooltip label="source" description="Expressão JMESPath." />)

    const trigger = screen.getByRole("button", { name: /ajuda: source/i })
    fireEvent.focus(trigger)

    expect(screen.getByRole("tooltip")).toBeInTheDocument()
  })

  it("fecha o tooltip ao perder o foco (blur)", () => {
    render(<HelpTooltip label="source" description="Expressão JMESPath." />)

    const trigger = screen.getByRole("button", { name: /ajuda: source/i })
    fireEvent.focus(trigger)
    expect(screen.getByRole("tooltip")).toBeInTheDocument()

    fireEvent.blur(trigger, { relatedTarget: null })
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument()
  })

  it("renderiza example em <code> quando prop presente", () => {
    render(
      <HelpTooltip
        label="target"
        description="Caminho no envelope."
        example="normalized.severity_id"
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: /ajuda: target/i }))

    const codeEl = screen.getByText("normalized.severity_id")
    expect(codeEl.tagName).toBe("CODE")
  })

  it("não renderiza example quando prop ausente", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope." />)

    fireEvent.click(screen.getByRole("button", { name: /ajuda: target/i }))

    expect(screen.queryByRole("code")).not.toBeInTheDocument()
  })

  it("renderiza link 'Saiba mais' quando learnMoreHref presente", () => {
    render(
      <HelpTooltip
        label="target"
        description="Caminho no envelope."
        learnMoreHref="/docs/guide.md"
      />,
    )

    fireEvent.click(screen.getByRole("button", { name: /ajuda: target/i }))

    const link = screen.getByRole("link", { name: /saiba mais/i })
    expect(link).toBeInTheDocument()
    expect(link).toHaveAttribute("href", "/docs/guide.md")
    expect(link).toHaveAttribute("target", "_blank")
  })

  it("não renderiza link quando learnMoreHref ausente", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope." />)

    fireEvent.click(screen.getByRole("button", { name: /ajuda: target/i }))

    expect(screen.queryByRole("link")).not.toBeInTheDocument()
  })

  it("tooltip tem role=tooltip", () => {
    render(<HelpTooltip label="target" description="Caminho no envelope." />)

    fireEvent.click(screen.getByRole("button", { name: /ajuda: target/i }))

    const tooltip = screen.getByRole("tooltip")
    expect(tooltip).toBeInTheDocument()
  })

  it("abre ao mouseenter e fecha ao mouseleave (após debounce de 120ms)", async () => {
    vi.useFakeTimers()
    try {
      render(<HelpTooltip label="target" description="Caminho no envelope." />)

      const trigger = screen.getByRole("button", { name: /ajuda: target/i })
      fireEvent.mouseEnter(trigger)
      expect(screen.getByRole("tooltip")).toBeInTheDocument()

      // mouseLeave agora agenda close (não fecha imediatamente) — dá tempo
      // do cursor transitar do trigger pro tooltip via portal sem fechar.
      fireEvent.mouseLeave(trigger)
      // Antes do debounce expirar, ainda está aberto
      expect(screen.getByRole("tooltip")).toBeInTheDocument()

      // Avança o relógio além do delay (120ms) dentro de act() pra garantir
      // que o re-render dispare antes da assertion.
      await act(async () => {
        vi.advanceTimersByTime(150)
      })
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it("hover do trigger pro tooltip mantém aberto (sem piscar)", async () => {
    vi.useFakeTimers()
    try {
      render(<HelpTooltip label="target" description="Caminho no envelope." />)

      const trigger = screen.getByRole("button", { name: /ajuda: target/i })
      fireEvent.mouseEnter(trigger)
      const tooltip = screen.getByRole("tooltip")
      expect(tooltip).toBeInTheDocument()

      // Cursor sai do trigger e entra no tooltip — antes do debounce
      // expirar, o tooltip cancela o timer.
      fireEvent.mouseLeave(trigger)
      fireEvent.mouseEnter(tooltip)
      await act(async () => {
        vi.advanceTimersByTime(200)
      })

      // Tooltip permanece aberto.
      expect(screen.queryByRole("tooltip")).toBeInTheDocument()

      // Sai do tooltip — agenda close de novo.
      fireEvent.mouseLeave(tooltip)
      await act(async () => {
        vi.advanceTimersByTime(200)
      })
      expect(screen.queryByRole("tooltip")).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
