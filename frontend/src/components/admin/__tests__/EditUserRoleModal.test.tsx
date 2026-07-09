import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { EditUserRoleModal } from "@/components/admin/EditUserRoleModal"
import type { AppUser } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const fakeUser: AppUser = {
  id: "42",
  username: "alice",
  display_name: "Alice",
  role: "engineer",
  is_active: true,
  permissions: ["mapping.write"],
  organization_id: null,
  organization_name: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
}

describe("EditUserRoleModal", () => {
  it("não renderiza quando fechado", () => {
    render(
      <EditUserRoleModal
        open={false}
        user={fakeUser}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    )
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("mostra select com role atual pré-selecionado quando aberto", () => {
    render(
      <EditUserRoleModal
        open={true}
        user={fakeUser}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    )
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    const select = screen.getByTestId("role-select") as HTMLSelectElement
    expect(select).toBeInTheDocument()
    expect(select.value).toBe("engineer")
  })

  it("botão Salvar desabilitado quando role não mudou", () => {
    render(
      <EditUserRoleModal
        open={true}
        user={fakeUser}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    )
    const saveBtn = screen.getByRole("button", { name: /salvar papel/i })
    expect(saveBtn).toBeDisabled()
  })

  it("habilita botão Salvar quando role muda", () => {
    render(
      <EditUserRoleModal
        open={true}
        user={fakeUser}
        onClose={vi.fn()}
        onSave={vi.fn()}
      />,
    )
    const select = screen.getByTestId("role-select")
    fireEvent.change(select, { target: { value: "admin" } })
    const saveBtn = screen.getByRole("button", { name: /salvar papel/i })
    expect(saveBtn).not.toBeDisabled()
  })

  it("chama onSave com userId, novo role e razão ao submeter", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    const onClose = vi.fn()

    render(
      <EditUserRoleModal
        open={true}
        user={fakeUser}
        onClose={onClose}
        onSave={onSave}
      />,
    )

    // Muda o papel
    fireEvent.change(screen.getByTestId("role-select"), { target: { value: "admin" } })

    // Preenche motivo
    const textarea = screen.getByPlaceholderText(/promovido a engenheiro/i)
    fireEvent.change(textarea, { target: { value: "promovido na sprint 4" } })

    fireEvent.click(screen.getByRole("button", { name: /salvar papel/i }))

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith("42", "admin", "promovido na sprint 4")
    })
    expect(onClose).toHaveBeenCalled()
  })

  it("mostra erro quando onSave rejeita", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("Sem permissão"))

    render(
      <EditUserRoleModal
        open={true}
        user={fakeUser}
        onClose={vi.fn()}
        onSave={onSave}
      />,
    )

    fireEvent.change(screen.getByTestId("role-select"), { target: { value: "admin" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar papel/i }))

    await waitFor(() => {
      expect(screen.getByText("Sem permissão")).toBeInTheDocument()
    })
  })

  it("fecha ao clicar em Cancelar", () => {
    const onClose = vi.fn()
    render(
      <EditUserRoleModal
        open={true}
        user={fakeUser}
        onClose={onClose}
        onSave={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /cancelar/i }))
    expect(onClose).toHaveBeenCalled()
  })
})
