/**
 * Testes de TemplatePicker
 * Cobre: render, pick com editor vazio, confirmação com regras existentes,
 * cancelamento, Escape para fechar.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { TemplatePicker } from "@/components/mappings/TemplatePicker"
import { OCSF_TEMPLATES } from "@/data/ocsfTemplates"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const DEFAULT_PROPS = {
  open: true,
  onClose: vi.fn(),
  onPick: vi.fn(),
  existingRulesCount: 0,
}

function renderPicker(props?: Partial<typeof DEFAULT_PROPS>) {
  return render(<TemplatePicker {...DEFAULT_PROPS} {...props} />)
}

beforeEach(() => {
  vi.clearAllMocks()
})

// ── Render ────────────────────────────────────────────────────────────────────

describe("TemplatePicker — render", () => {
  it("renderiza o modal quando open=true", () => {
    renderPicker()
    expect(screen.getByTestId("template-picker")).toBeInTheDocument()
  })

  it("NÃO renderiza quando open=false", () => {
    renderPicker({ open: false })
    expect(screen.queryByTestId("template-picker")).not.toBeInTheDocument()
  })

  it("exibe todos os templates disponíveis (3)", () => {
    renderPicker()
    expect(screen.getByTestId("template-list")).toBeInTheDocument()
    for (const template of OCSF_TEMPLATES) {
      expect(screen.getByTestId(`template-card-${template.id}`)).toBeInTheDocument()
      expect(screen.getByText(template.name)).toBeInTheDocument()
      expect(screen.getByText(template.description)).toBeInTheDocument()
    }
  })

  it("cada card tem um botão 'Usar template' (não div com onClick)", () => {
    renderPicker()
    for (const template of OCSF_TEMPLATES) {
      const btn = screen.getByTestId(`use-template-${template.id}`)
      expect(btn.tagName).toBe("BUTTON")
      expect(btn).toHaveAttribute("type", "button")
    }
  })

  it("modal tem título 'Carregar template OCSF'", () => {
    renderPicker()
    expect(screen.getByText("Carregar template OCSF")).toBeInTheDocument()
  })
})

// ── Pick sem regras existentes ─────────────────────────────────────────────────

describe("TemplatePicker — pick sem regras existentes", () => {
  it("clicar 'Usar template' com editor vazio chama onPick com o template correto", () => {
    const onPick = vi.fn()
    renderPicker({ existingRulesCount: 0, onPick })

    const firstTemplate = OCSF_TEMPLATES[0]
    fireEvent.click(screen.getByTestId(`use-template-${firstTemplate.id}`))

    expect(onPick).toHaveBeenCalledTimes(1)
    expect(onPick).toHaveBeenCalledWith(firstTemplate)
  })

  it("onClose é chamado após pick com editor vazio", () => {
    const onClose = vi.fn()
    renderPicker({ existingRulesCount: 0, onClose })

    fireEvent.click(screen.getByTestId(`use-template-${OCSF_TEMPLATES[0].id}`))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("NÃO exibe o diálogo de confirmação com editor vazio", () => {
    renderPicker({ existingRulesCount: 0 })
    expect(screen.queryByTestId("template-confirm")).not.toBeInTheDocument()
  })
})

// ── Confirmação com regras existentes ─────────────────────────────────────────

describe("TemplatePicker — confirmação com regras existentes", () => {
  it("exibe prompt de confirmação ao clicar 'Usar template' com regras existentes", () => {
    renderPicker({ existingRulesCount: 3 })

    fireEvent.click(screen.getByTestId(`use-template-${OCSF_TEMPLATES[0].id}`))

    expect(screen.getByTestId("template-confirm")).toBeInTheDocument()
    expect(screen.getByText(/Substituir 3 regras existentes/)).toBeInTheDocument()
  })

  it("prompt exibe o nome do template selecionado dentro do bloco de confirmação", () => {
    renderPicker({ existingRulesCount: 2 })

    const template = OCSF_TEMPLATES[1]
    fireEvent.click(screen.getByTestId(`use-template-${template.id}`))

    const confirm = screen.getByTestId("template-confirm")
    // O nome aparece em bold dentro do prompt de confirmação
    expect(confirm).toHaveTextContent(template.name)
  })

  it("'Substituir' chama onPick com o template e fecha o modal", () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    renderPicker({ existingRulesCount: 5, onPick, onClose })

    const template = OCSF_TEMPLATES[0]
    fireEvent.click(screen.getByTestId(`use-template-${template.id}`))
    fireEvent.click(screen.getByTestId("template-confirm-replace"))

    expect(onPick).toHaveBeenCalledTimes(1)
    expect(onPick).toHaveBeenCalledWith(template)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("'Cancelar' NÃO chama onPick e fecha apenas o prompt de confirmação (modal permanece)", () => {
    const onPick = vi.fn()
    const onClose = vi.fn()
    renderPicker({ existingRulesCount: 4, onPick, onClose })

    fireEvent.click(screen.getByTestId(`use-template-${OCSF_TEMPLATES[0].id}`))
    expect(screen.getByTestId("template-confirm")).toBeInTheDocument()

    fireEvent.click(screen.getByTestId("template-confirm-cancel"))

    expect(onPick).not.toHaveBeenCalled()
    expect(onClose).not.toHaveBeenCalled()
    // O prompt some mas o modal permanece
    expect(screen.queryByTestId("template-confirm")).not.toBeInTheDocument()
    expect(screen.getByTestId("template-picker")).toBeInTheDocument()
  })

  it("singular 'regra existente' para existingRulesCount=1", () => {
    renderPicker({ existingRulesCount: 1 })

    fireEvent.click(screen.getByTestId(`use-template-${OCSF_TEMPLATES[0].id}`))

    expect(screen.getByText(/Substituir 1 regra existente/)).toBeInTheDocument()
  })
})

// ── Acessibilidade ────────────────────────────────────────────────────────────

describe("TemplatePicker — acessibilidade", () => {
  it("modal tem role=dialog e aria-modal=true (via Modal component)", () => {
    renderPicker()
    // O Modal component renderiza role=dialog no overlay com aria-modal=true
    const dialog = screen.getByRole("dialog")
    expect(dialog).toBeInTheDocument()
    expect(dialog).toHaveAttribute("aria-modal", "true")
    // O Modal renderiza aria-labelledby quando tem title
    expect(dialog).toHaveAttribute("aria-labelledby")
  })

  it("Escape fecha o modal quando não há confirmação pendente", () => {
    const onClose = vi.fn()
    renderPicker({ onClose, existingRulesCount: 0 })

    fireEvent.keyDown(document, { key: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("botões 'Usar template' são elementos <button>, não divs", () => {
    renderPicker()
    for (const template of OCSF_TEMPLATES) {
      const el = screen.getByTestId(`use-template-${template.id}`)
      expect(el.tagName).toBe("BUTTON")
    }
  })
})
