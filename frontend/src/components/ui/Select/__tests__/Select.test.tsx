import { describe, it, expect, vi } from "vitest"
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { Select } from "../Select"

const OPTIONS = [
  { value: "drop_newest", label: "drop_newest" },
  { value: "drop_oldest", label: "drop_oldest" },
  { value: "block", label: "block" },
]

describe("Select", () => {
  it("abre a lista de opções ao clicar no trigger", () => {
    render(<Select options={OPTIONS} aria-label="backpressure" />)
    expect(screen.queryByRole("listbox")).toBeNull()
    fireEvent.click(screen.getByLabelText("backpressure"))
    const listbox = screen.getByRole("listbox")
    expect(within(listbox).getAllByRole("option")).toHaveLength(3)
  })

  it("emite o valor selecionado", () => {
    const onChange = vi.fn()
    render(<Select options={OPTIONS} aria-label="tier" onChange={onChange} />)
    fireEvent.click(screen.getByLabelText("tier"))
    fireEvent.click(screen.getByRole("option", { name: "block" }))
    expect(onChange).toHaveBeenCalledWith("block")
  })

  it("o dropdown abre na CAMADA popover (acima do modal) — regressão do select atrás do modal", () => {
    // Bug: --z-index-dropdown (1000) < --z-index-modal (1050) ⇒ a lista abria
    // ATRÁS do Modal. Fix: o portal usa var(--z-index-popover) (1060 > 1050).
    render(<Select options={OPTIONS} aria-label="destino" />)
    fireEvent.click(screen.getByLabelText("destino"))
    const portal = screen.getByRole("listbox").closest(".animate-slide-down") as HTMLElement
    expect(portal).not.toBeNull()
    expect(portal.style.zIndex).toBe("var(--z-index-popover)")
  })

  // R2-8.4: com uma opção desabilitada ANTES do alvo, o índice de
  // `filteredOptions` (todas) nunca batia com o índice do NodeList de botões
  // habilitados — setas/type-ahead/foco inicial pulavam pra opção errada a
  // partir dali. Fix: navegar por VALOR, não por índice posicional.
  describe("navegação com opção desabilitada antes do alvo", () => {
    const OPTIONS_WITH_DISABLED = [
      { value: "a", label: "alpha" },
      { value: "b", label: "bravo", disabled: true },
      { value: "c", label: "charlie" },
      { value: "d", label: "delta" },
    ]

    // O foco pra dentro do portal roda num `window.setTimeout(0)` (o ref só
    // existe após o commit) — as asserções esperam por ele via `waitFor` e
    // checam `role="option"` explicitamente (checar só o texto em
    // `document.activeElement` é uma armadilha: antes do timeout rodar,
    // `document.activeElement` é o `<body>`, que TAMBÉM "contém" o texto de
    // qualquer opção em algum lugar da árvore — passaria por vacuidade).
    async function waitForOptionFocus() {
      await waitFor(() => expect(document.activeElement).toHaveAttribute("role", "option"))
      return document.activeElement as HTMLElement
    }

    it("foco inicial ao abrir pousa na opção SELECIONADA (depois da desabilitada), não na vizinha errada", async () => {
      render(<Select options={OPTIONS_WITH_DISABLED} aria-label="sel" value="c" />)
      fireEvent.click(screen.getByLabelText("sel"))
      const focused = await waitForOptionFocus()
      expect(focused).toHaveTextContent("charlie")
    })

    it("ArrowDown a partir da opção logo ANTES da desabilitada pula pra próxima HABILITADA (não trava)", async () => {
      render(<Select options={OPTIONS_WITH_DISABLED} aria-label="sel" />)
      fireEvent.click(screen.getByLabelText("sel"))
      // Foco inicial: nenhuma selecionada → 1ª habilitada (alpha).
      const first = await waitForOptionFocus()
      expect(first).toHaveTextContent("alpha")
      fireEvent.keyDown(first, { key: "ArrowDown" })
      // Pula "bravo" (desabilitada) direto pra "charlie".
      expect(document.activeElement).toHaveAttribute("role", "option")
      expect(document.activeElement).toHaveTextContent("charlie")
    })

    it("ArrowDown a partir da opção logo DEPOIS da desabilitada avança normalmente pra seguinte", async () => {
      render(<Select options={OPTIONS_WITH_DISABLED} aria-label="sel" value="c" />)
      fireEvent.click(screen.getByLabelText("sel"))
      const charlie = await waitForOptionFocus()
      expect(charlie).toHaveTextContent("charlie")
      fireEvent.keyDown(charlie, { key: "ArrowDown" })
      expect(document.activeElement).toHaveAttribute("role", "option")
      expect(document.activeElement).toHaveTextContent("delta")
    })

    it("Enter na opção focada seleciona ela mesma (não uma opção vizinha)", async () => {
      const onChange = vi.fn()
      render(<Select options={OPTIONS_WITH_DISABLED} aria-label="sel" value="c" onChange={onChange} />)
      fireEvent.click(screen.getByLabelText("sel"))
      const charlie = await waitForOptionFocus()
      expect(charlie).toHaveTextContent("charlie")
      fireEvent.keyDown(charlie, { key: "Enter" })
      expect(onChange).toHaveBeenCalledWith("c")
    })
  })
})
