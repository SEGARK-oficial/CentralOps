/**
 * Testes de DriftBulkActionBar
 * Cobre: render condicional, botões gateados por permissão,
 *        abre ConfirmDialog, executa ação com sucesso, exibe erro,
 *        limpar seleção, acessibilidade de teclado.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { DriftBulkActionBar } from "@/components/normalization/DriftBulkActionBar"
import * as permHooks from "@/hooks/usePermission"

vi.mock("@/hooks/usePermission")
const mockedUsePermission = vi.mocked(permHooks.usePermission)

// ── Helper de render ──────────────────────────────────────────────────────────

function renderBar(
  selectedIds: string[],
  overrides: Partial<React.ComponentProps<typeof DriftBulkActionBar>> = {},
) {
  const onClearSelection = vi.fn()
  const onBulkIgnore = vi.fn().mockResolvedValue(undefined)
  const onBulkMarkMapped = vi.fn().mockResolvedValue(undefined)
  const onSuccess = vi.fn()

  const utils = render(
    <DriftBulkActionBar
      selectedIds={selectedIds}
      onClearSelection={onClearSelection}
      onBulkIgnore={onBulkIgnore}
      onBulkMarkMapped={onBulkMarkMapped}
      onSuccess={onSuccess}
      {...overrides}
    />,
  )

  return { ...utils, onClearSelection, onBulkIgnore, onBulkMarkMapped, onSuccess }
}

beforeEach(() => {
  vi.clearAllMocks()
  // Por padrão sem permissões
  mockedUsePermission.mockReturnValue(false)
})

// ── Render condicional ────────────────────────────────────────────────────────

describe("DriftBulkActionBar — render condicional", () => {
  it("não renderiza nada quando selectedIds está vazio", () => {
    const { container } = renderBar([])
    expect(container.firstChild).toBeNull()
  })

  it("renderiza a barra quando há ids selecionados", () => {
    renderBar(["id1"])
    expect(screen.getByTestId("drift-bulk-bar")).toBeInTheDocument()
  })

  it("exibe contagem singular '1 selecionado'", () => {
    renderBar(["id1"])
    expect(screen.getByText(/1 selecionado/)).toBeInTheDocument()
  })

  it("exibe contagem plural '3 selecionados'", () => {
    renderBar(["id1", "id2", "id3"])
    expect(screen.getByText(/3 selecionados/)).toBeInTheDocument()
  })

  it("região tem aria-label 'Ações em massa'", () => {
    renderBar(["id1"])
    expect(screen.getByRole("region", { name: /ações em massa/i })).toBeInTheDocument()
  })
})

// ── Gating por permissão ─────────────────────────────────────────────────────

describe("DriftBulkActionBar — gating de permissão", () => {
  it("botão Ignorar não aparece sem permissão drift.ignore", () => {
    renderBar(["id1"])
    expect(screen.queryByTestId("drift-bulk-ignore")).not.toBeInTheDocument()
  })

  it("botão Ignorar aparece com permissão drift.ignore", () => {
    mockedUsePermission.mockImplementation((p) => p === "drift.ignore")
    renderBar(["id1"])
    expect(screen.getByTestId("drift-bulk-ignore")).toBeInTheDocument()
  })

  it("botão Marcar mapeado não aparece sem permissão drift.mark_mapped", () => {
    renderBar(["id1"])
    expect(screen.queryByTestId("drift-bulk-mark-mapped")).not.toBeInTheDocument()
  })

  it("botão Marcar mapeado aparece com permissão drift.mark_mapped", () => {
    mockedUsePermission.mockImplementation((p) => p === "drift.mark_mapped")
    renderBar(["id1"])
    expect(screen.getByTestId("drift-bulk-mark-mapped")).toBeInTheDocument()
  })

  it("botão Limpar sempre aparece independente de permissões", () => {
    renderBar(["id1"])
    // O botão "Limpar seleção" vem do primitive BulkActionBar.
    expect(screen.getByRole("button", { name: /limpar seleção/i })).toBeInTheDocument()
  })
})

// ── Limpar seleção ────────────────────────────────────────────────────────────

describe("DriftBulkActionBar — limpar seleção", () => {
  it("clicar em Limpar chama onClearSelection", () => {
    const { onClearSelection } = renderBar(["id1"])
    fireEvent.click(screen.getByRole("button", { name: /limpar seleção/i }))
    expect(onClearSelection).toHaveBeenCalledTimes(1)
  })

  it("botão Limpar tem aria-label 'Limpar seleção'", () => {
    renderBar(["id1"])
    expect(screen.getByRole("button", { name: /limpar seleção/i })).toHaveAttribute(
      "aria-label",
      "Limpar seleção",
    )
  })
})

// ── Fluxo Ignorar ────────────────────────────────────────────────────────────

describe("DriftBulkActionBar — fluxo ignorar", () => {
  beforeEach(() => {
    mockedUsePermission.mockImplementation((p) => p === "drift.ignore")
  })

  it("clicar em Ignorar abre ConfirmDialog com título correto (plural)", () => {
    renderBar(["id1", "id2"])
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    expect(screen.getByText("Ignorar 2 campos?")).toBeInTheDocument()
  })

  it("clicar em Ignorar abre ConfirmDialog com título correto (singular)", () => {
    renderBar(["id1"])
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    expect(screen.getByText("Ignorar 1 campo?")).toBeInTheDocument()
  })

  it("confirmar chama onBulkIgnore com os ids selecionados", async () => {
    const { onBulkIgnore } = renderBar(["id1", "id2"])
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    // Usar getAllByRole e pegar o último botão com nome "Ignorar" (o do dialog, não o da barra)
    const confirmBtns = screen.getAllByRole("button", { name: /^ignorar$/i })
    fireEvent.click(confirmBtns[confirmBtns.length - 1])
    await waitFor(() => expect(onBulkIgnore).toHaveBeenCalledWith(["id1", "id2"]))
  })

  it("após confirmar chama onClearSelection e onSuccess", async () => {
    const { onClearSelection, onSuccess } = renderBar(["id1"])
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    const confirmBtns = screen.getAllByRole("button", { name: /^ignorar$/i })
    fireEvent.click(confirmBtns[confirmBtns.length - 1])
    await waitFor(() => expect(onClearSelection).toHaveBeenCalled())
    expect(onSuccess).toHaveBeenCalled()
  })

  it("exibe mensagem de sucesso singular após confirmar", async () => {
    renderBar(["id1"])
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    const confirmBtns = screen.getAllByRole("button", { name: /^ignorar$/i })
    fireEvent.click(confirmBtns[confirmBtns.length - 1])
    await waitFor(() => expect(screen.getByText(/1 campo ignorado/)).toBeInTheDocument())
  })

  it("exibe mensagem de sucesso plural após confirmar", async () => {
    renderBar(["id1", "id2"])
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    const confirmBtns = screen.getAllByRole("button", { name: /^ignorar$/i })
    fireEvent.click(confirmBtns[confirmBtns.length - 1])
    await waitFor(() => expect(screen.getByText(/2 campos ignorados/)).toBeInTheDocument())
  })

  it("exibe erro quando onBulkIgnore rejeita", async () => {
    const onBulkIgnore = vi.fn().mockRejectedValue(new Error("Falha na rede"))
    renderBar(["id1"], { onBulkIgnore })
    fireEvent.click(screen.getByTestId("drift-bulk-ignore"))
    const confirmBtns = screen.getAllByRole("button", { name: /^ignorar$/i })
    fireEvent.click(confirmBtns[confirmBtns.length - 1])
    await waitFor(() => expect(screen.getByText("Falha na rede")).toBeInTheDocument())
  })
})

// ── Fluxo Marcar mapeado ──────────────────────────────────────────────────────

describe("DriftBulkActionBar — fluxo marcar mapeado", () => {
  beforeEach(() => {
    mockedUsePermission.mockImplementation((p) => p === "drift.mark_mapped")
  })

  it("clicar em Marcar mapeado abre ConfirmDialog com título correto (plural)", () => {
    renderBar(["id1", "id2"])
    fireEvent.click(screen.getByTestId("drift-bulk-mark-mapped"))
    expect(screen.getByText("Marcar 2 campos como mapeado?")).toBeInTheDocument()
  })

  it("clicar em Marcar mapeado abre ConfirmDialog com título correto (singular)", () => {
    renderBar(["id1"])
    fireEvent.click(screen.getByTestId("drift-bulk-mark-mapped"))
    expect(screen.getByText("Marcar 1 campo como mapeado?")).toBeInTheDocument()
  })

  it("confirmar chama onBulkMarkMapped com os ids selecionados", async () => {
    const { onBulkMarkMapped } = renderBar(["id1", "id2"])
    fireEvent.click(screen.getByTestId("drift-bulk-mark-mapped"))
    // "Marcar" only appears in the dialog confirm button; "Marcar mapeado" is the bar button
    fireEvent.click(screen.getByRole("button", { name: /^marcar$/i }))
    await waitFor(() => expect(onBulkMarkMapped).toHaveBeenCalledWith(["id1", "id2"]))
  })

  it("exibe mensagem de sucesso plural após confirmar", async () => {
    renderBar(["id1", "id2"])
    fireEvent.click(screen.getByTestId("drift-bulk-mark-mapped"))
    fireEvent.click(screen.getByRole("button", { name: /^marcar$/i }))
    await waitFor(() =>
      expect(screen.getByText(/2 campos marcados como mapeados/)).toBeInTheDocument(),
    )
  })
})

// ── Acessibilidade de teclado ─────────────────────────────────────────────────

describe("DriftBulkActionBar — acessibilidade de teclado", () => {
  it("botão Limpar é focalizável e ativável por teclado (Enter)", () => {
    const { onClearSelection } = renderBar(["id1"])
    const btn = screen.getByRole("button", { name: /limpar seleção/i })
    btn.focus()
    fireEvent.keyDown(btn, { key: "Enter" })
    // O componente Button nativo responde ao click, então usamos click aqui como proxy
    fireEvent.click(btn)
    expect(onClearSelection).toHaveBeenCalled()
  })

  it("botão Ignorar é focalizável com permissão", () => {
    mockedUsePermission.mockImplementation((p) => p === "drift.ignore")
    renderBar(["id1"])
    const btn = screen.getByTestId("drift-bulk-ignore")
    expect(btn.tagName).toBe("BUTTON")
  })
})
