/**
 * Testes da CommandPalette:
 * - Abre via evento Cmd+K / Ctrl+K
 * - Fecha via Esc
 * - Render padrão (lista de grupos/itens)
 * - Filtragem por label e keywords
 * - Navegação por setas Up/Down
 * - Enter executa o comando selecionado
 * - Acessibilidade: role=dialog, aria-modal, aria-activedescendant
 */

import { fireEvent, render, screen, act } from "@testing-library/react"
import { CommandPalette } from "@/components/ui/CommandPalette"
import type { PaletteCommand } from "@/components/ui/CommandPalette"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const COMMANDS: PaletteCommand[] = [
  {
    id: "nav-dashboard",
    label: "Dashboard",
    group: "Navegar",
    keywords: ["home", "inicio"],
    run: vi.fn(),
  },
  {
    id: "nav-integrations",
    label: "Integrações",
    group: "Navegar",
    keywords: ["integrations"],
    run: vi.fn(),
  },
  {
    id: "nav-collectors",
    label: "Coletores",
    group: "Navegar",
    keywords: ["collectors"],
    run: vi.fn(),
  },
  {
    id: "action-refresh",
    label: "Atualizar dados",
    group: "Ações",
    keywords: ["refresh", "reload"],
    run: vi.fn(),
  },
]

function renderPalette(props?: Partial<React.ComponentProps<typeof CommandPalette>>) {
  return render(<CommandPalette commands={COMMANDS} {...props} />)
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Simula Cmd+K (metaKey) para abrir a paleta. */
function pressOpenShortcut() {
  fireEvent.keyDown(window, { key: "k", metaKey: true })
}

/** Simula Ctrl+K para abrir a paleta. */
function pressOpenShortcutCtrl() {
  fireEvent.keyDown(window, { key: "k", ctrlKey: true })
}

// ---------------------------------------------------------------------------
// Suites
// ---------------------------------------------------------------------------

describe("CommandPalette — abertura", () => {
  it("não renderiza dialog por padrão (fechado)", () => {
    renderPalette()
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("abre com Cmd+K", () => {
    renderPalette()
    pressOpenShortcut()
    expect(screen.getByRole("dialog")).toBeInTheDocument()
  })

  it("abre com Ctrl+K", () => {
    renderPalette()
    pressOpenShortcutCtrl()
    expect(screen.getByRole("dialog")).toBeInTheDocument()
  })

  it("modo controlado — abre quando open=true", () => {
    renderPalette({ open: true, onOpenChange: () => {} })
    expect(screen.getByRole("dialog")).toBeInTheDocument()
  })

  it("modo controlado — fecha quando open=false", () => {
    renderPalette({ open: false, onOpenChange: () => {} })
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })
})

describe("CommandPalette — fechamento", () => {
  it("fecha ao pressionar Esc dentro do dialog", () => {
    renderPalette()
    pressOpenShortcut()
    expect(screen.getByRole("dialog")).toBeInTheDocument()

    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("fecha ao clicar no overlay (fora do dialog)", () => {
    renderPalette()
    pressOpenShortcut()

    // O overlay é o elemento pai com fixed inset-0
    // O overlay é o primeiro filho de body injetado pelo portal
    const overlay = document.querySelector(".fixed.inset-0") as HTMLElement
    expect(overlay).not.toBeNull()
    // Simula clique no próprio overlay (currentTarget === target)
    fireEvent.click(overlay, { target: overlay })
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("chama onOpenChange(false) no modo controlado ao pressionar Esc", () => {
    const onOpenChange = vi.fn()
    renderPalette({ open: true, onOpenChange })
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })
})

describe("CommandPalette — render de comandos", () => {
  it("exibe os grupos", () => {
    renderPalette()
    pressOpenShortcut()
    // Grupos aparecem como cabeçalhos de seção (texto visível)
    expect(screen.getAllByText("Navegar").length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText("Ações").length).toBeGreaterThanOrEqual(1)
  })

  it("exibe os labels dos comandos", () => {
    renderPalette()
    pressOpenShortcut()
    expect(screen.getByText("Dashboard")).toBeInTheDocument()
    expect(screen.getByText("Integrações")).toBeInTheDocument()
    expect(screen.getByText("Coletores")).toBeInTheDocument()
    expect(screen.getByText("Atualizar dados")).toBeInTheDocument()
  })

  it("dialog tem aria-modal=true", () => {
    renderPalette()
    pressOpenShortcut()
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true")
  })
})

describe("CommandPalette — filtragem", () => {
  it("filtra por label (substring case-insensitive)", () => {
    renderPalette()
    pressOpenShortcut()
    const input = screen.getByRole("combobox")
    fireEvent.change(input, { target: { value: "cole" } })

    expect(screen.getByText("Coletores")).toBeInTheDocument()
    expect(screen.queryByText("Dashboard")).not.toBeInTheDocument()
    expect(screen.queryByText("Integrações")).not.toBeInTheDocument()
  })

  it("filtra por keyword", () => {
    renderPalette()
    pressOpenShortcut()
    const input = screen.getByRole("combobox")
    fireEvent.change(input, { target: { value: "reload" } })

    expect(screen.getByText("Atualizar dados")).toBeInTheDocument()
    expect(screen.queryByText("Dashboard")).not.toBeInTheDocument()
  })

  it("exibe mensagem quando sem resultados", () => {
    renderPalette()
    pressOpenShortcut()
    const input = screen.getByRole("combobox")
    fireEvent.change(input, { target: { value: "xyzxyz" } })

    expect(screen.getByText(/Nenhum resultado/i)).toBeInTheDocument()
  })
})

describe("CommandPalette — navegação por teclado", () => {
  it("ArrowDown move seleção para o próximo item", () => {
    renderPalette()
    pressOpenShortcut()
    const dialog = screen.getByRole("dialog")

    // Item 0 (Dashboard) está ativo inicialmente
    // ArrowDown move para item 1 (Integrações)
    fireEvent.keyDown(dialog, { key: "ArrowDown" })

    const input = screen.getByRole("combobox")
    // aria-activedescendant aponta para o segundo item
    const activeId = input.getAttribute("aria-activedescendant")
    expect(activeId).toBe("cp-item-nav-integrations")
  })

  it("ArrowUp não vai abaixo de 0", () => {
    renderPalette()
    pressOpenShortcut()
    const dialog = screen.getByRole("dialog")
    // Pressionar Up quando já no primeiro item não quebra
    fireEvent.keyDown(dialog, { key: "ArrowUp" })

    const input = screen.getByRole("combobox")
    const activeId = input.getAttribute("aria-activedescendant")
    // Continua no primeiro item
    expect(activeId).toBe("cp-item-nav-dashboard")
  })

  it("ArrowDown + ArrowDown navega para o terceiro item", () => {
    renderPalette()
    pressOpenShortcut()
    const dialog = screen.getByRole("dialog")

    fireEvent.keyDown(dialog, { key: "ArrowDown" })
    fireEvent.keyDown(dialog, { key: "ArrowDown" })

    const input = screen.getByRole("combobox")
    const activeId = input.getAttribute("aria-activedescendant")
    expect(activeId).toBe("cp-item-nav-collectors")
  })
})

describe("CommandPalette — execução", () => {
  it("Enter executa o comando ativo e fecha o dialog", () => {
    const runFn = vi.fn()
    const cmds: PaletteCommand[] = [
      { id: "test-cmd", label: "Testar ação", group: "Ações", run: runFn },
    ]
    render(<CommandPalette commands={cmds} />)
    pressOpenShortcut()

    const dialog = screen.getByRole("dialog")
    fireEvent.keyDown(dialog, { key: "Enter" })

    expect(runFn).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("clicar em um item executa run e fecha", () => {
    const runFn = vi.fn()
    const cmds: PaletteCommand[] = [
      { id: "click-cmd", label: "Clicar aqui", group: "Ações", run: runFn },
    ]
    render(<CommandPalette commands={cmds} />)
    pressOpenShortcut()

    fireEvent.click(screen.getByText("Clicar aqui"))
    expect(runFn).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("Enter sem itens filtrados não lança erro", () => {
    renderPalette()
    pressOpenShortcut()
    const input = screen.getByRole("combobox")
    fireEvent.change(input, { target: { value: "xyzxyz" } })

    expect(() => {
      fireEvent.keyDown(screen.getByRole("dialog"), { key: "Enter" })
    }).not.toThrow()
  })
})

describe("CommandPalette — acessibilidade", () => {
  it("input tem role=combobox e aria-expanded=true", () => {
    renderPalette()
    pressOpenShortcut()
    const input = screen.getByRole("combobox")
    expect(input).toHaveAttribute("aria-expanded", "true")
  })

  it("item ativo tem aria-selected=true", () => {
    renderPalette()
    pressOpenShortcut()
    const activeItem = document.getElementById("cp-item-nav-dashboard")
    expect(activeItem).toHaveAttribute("aria-selected", "true")
  })

  it("itens inativos têm aria-selected=false", () => {
    renderPalette()
    pressOpenShortcut()
    const inactiveItem = document.getElementById("cp-item-nav-integrations")
    expect(inactiveItem).toHaveAttribute("aria-selected", "false")
  })

  it("listbox é referenciado por aria-controls do input", () => {
    renderPalette()
    pressOpenShortcut()
    const input = screen.getByRole("combobox")
    const controlsId = input.getAttribute("aria-controls")
    expect(controlsId).toBeTruthy()
    expect(document.getElementById(controlsId!)).not.toBeNull()
  })
})
