/**
 * Testes de DefaultHitWarningsPopover
 * Cobre: render lista, action "Marcar como intencional", fechar em Escape,
 * fechar em clique fora, estado defensivo sem warnings.
 * Fase 4.1b
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { DefaultHitWarningsPopover } from "@/components/mappings/DefaultHitWarningsPopover"
import type { DryRunDefaultHitWarning } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const WARNING_A: DryRunDefaultHitWarning = {
  target: "event.action",
  hit_rate: 1.0,
  hit_count: 5,
  sample_size: 5,
  expected_always_default: false,
}

const WARNING_B: DryRunDefaultHitWarning = {
  target: "event.user",
  hit_rate: 1.0,
  hit_count: 3,
  sample_size: 3,
  expected_always_default: false,
}

function renderPopover(
  warnings: DryRunDefaultHitWarning[] = [WARNING_A, WARNING_B],
  onMarkIntentional = vi.fn(),
  onClose = vi.fn(),
) {
  return render(
    <DefaultHitWarningsPopover
      warnings={warnings}
      onMarkIntentional={onMarkIntentional}
      onClose={onClose}
    />,
  )
}

describe("DefaultHitWarningsPopover — render", () => {
  it("tem role=dialog com aria-labelledby apontando para o título", () => {
    renderPopover()
    const dialog = screen.getByRole("dialog")
    expect(dialog).toBeInTheDocument()
    const labelledBy = dialog.getAttribute("aria-labelledby")
    expect(labelledBy).toBeTruthy()
    const heading = document.getElementById(labelledBy!)
    expect(heading).not.toBeNull()
    expect(heading!.textContent).toMatch(/100% de fallback/i)
  })

  it("renderiza um item por warning com o target visível", () => {
    renderPopover()
    expect(screen.getByText("event.action")).toBeInTheDocument()
    expect(screen.getByText("event.user")).toBeInTheDocument()
  })

  it("cada item exibe o count de amostras", () => {
    renderPopover([WARNING_A])
    expect(screen.getByText(/5\/5 amostras caíram no default/)).toBeInTheDocument()
  })

  it("renderiza botão 'Marcar como intencional' para cada item", () => {
    renderPopover()
    const btns = screen.getAllByRole("button", { name: /marcar como intencional/i })
    expect(btns).toHaveLength(2)
  })

  it("estado defensivo: sem warnings exibe mensagem 'Nenhum aviso.'", () => {
    renderPopover([])
    expect(screen.getByText("Nenhum aviso.")).toBeInTheDocument()
  })
})

describe("DefaultHitWarningsPopover — ação Marcar como intencional", () => {
  it("click em 'Marcar como intencional' emite onMarkIntentional com o target correto", () => {
    const onMarkIntentional = vi.fn()
    renderPopover([WARNING_A, WARNING_B], onMarkIntentional)

    fireEvent.click(screen.getByTestId("mark-intentional-event.action"))
    expect(onMarkIntentional).toHaveBeenCalledWith("event.action")
    expect(onMarkIntentional).toHaveBeenCalledTimes(1)
  })

  it("click no segundo item emite o target do segundo item", () => {
    const onMarkIntentional = vi.fn()
    renderPopover([WARNING_A, WARNING_B], onMarkIntentional)

    fireEvent.click(screen.getByTestId("mark-intentional-event.user"))
    expect(onMarkIntentional).toHaveBeenCalledWith("event.user")
  })
})

describe("DefaultHitWarningsPopover — fechamento", () => {
  it("botão X chama onClose", () => {
    const onClose = vi.fn()
    renderPopover([WARNING_A], vi.fn(), onClose)

    fireEvent.click(screen.getByRole("button", { name: /fechar painel de avisos/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("tecla Escape chama onClose", () => {
    const onClose = vi.fn()
    renderPopover([WARNING_A], vi.fn(), onClose)

    fireEvent.keyDown(document, { key: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("clique fora do painel chama onClose", () => {
    const onClose = vi.fn()
    renderPopover([WARNING_A], vi.fn(), onClose)

    // Dispara pointerdown fora do componente (no document.body)
    fireEvent.pointerDown(document.body)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("clique dentro do painel NÃO chama onClose", () => {
    const onClose = vi.fn()
    renderPopover([WARNING_A], vi.fn(), onClose)

    const dialog = screen.getByRole("dialog")
    fireEvent.pointerDown(dialog)
    expect(onClose).not.toHaveBeenCalled()
  })
})
