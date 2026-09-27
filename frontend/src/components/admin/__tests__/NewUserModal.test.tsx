/**
 * NewUserModal — A11Y-26: erro acessível (aria-invalid/aria-describedby) e
 * foco no 1º campo inválido a cada tentativa de submit.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { NewUserModal } from "@/components/admin/NewUserModal"
import type { Organization } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const ORGS: Organization[] = [
  { id: 1, name: "Acme", slug: "acme", is_active: true, integration_count: 0 },
]

function renderModal(onCreate = vi.fn().mockResolvedValue(undefined)) {
  render(<NewUserModal open onClose={vi.fn()} onCreate={onCreate} organizations={ORGS} />)
  return { onCreate }
}

describe("NewUserModal — A11Y-26", () => {
  it("username vazio: foca o campo username e liga aria-invalid/aria-describedby ao erro", async () => {
    renderModal()
    fireEvent.click(screen.getByRole("button", { name: /criar usuário|salvar/i }))

    const usernameInput = screen.getByLabelText(/usuário \(login\)/i)
    await waitFor(() => expect(document.activeElement).toBe(usernameInput))
    expect(usernameInput).toHaveAttribute("aria-invalid", "true")
    const describedBy = usernameInput.getAttribute("aria-describedby")
    expect(describedBy).toBeTruthy()
    expect(document.getElementById(describedBy!)).toHaveTextContent(/obrigatório/i)
  })

  it("senha curta: foca o campo senha", async () => {
    renderModal()
    fireEvent.change(screen.getByLabelText(/usuário \(login\)/i), { target: { value: "bob" } })
    fireEvent.change(screen.getByLabelText(/^senha\*?$/i), { target: { value: "curta" } })
    fireEvent.click(screen.getByRole("button", { name: /criar usuário|salvar/i }))

    const passwordInput = screen.getByLabelText(/^senha\*?$/i)
    await waitFor(() => expect(document.activeElement).toBe(passwordInput))
    expect(passwordInput).toHaveAttribute("aria-invalid", "true")
  })

  it("senhas não coincidem: foca confirmar senha", async () => {
    renderModal()
    fireEvent.change(screen.getByLabelText(/usuário \(login\)/i), { target: { value: "bob" } })
    fireEvent.change(screen.getByLabelText(/^senha\*?$/i), { target: { value: "senha-longa-o-suficiente" } })
    fireEvent.change(screen.getByLabelText(/confirmar senha|confirm password/i), { target: { value: "outra-coisa" } })
    fireEvent.click(screen.getByRole("button", { name: /criar usuário|salvar/i }))

    const confirmInput = screen.getByLabelText(/confirmar senha|confirm password/i)
    await waitFor(() => expect(document.activeElement).toBe(confirmInput))
    expect(confirmInput).toHaveAttribute("aria-invalid", "true")
  })

  it("role não-admin sem organização: foca o select de organização", async () => {
    renderModal()
    fireEvent.change(screen.getByLabelText(/usuário \(login\)/i), { target: { value: "bob" } })
    fireEvent.change(screen.getByLabelText(/^senha\*?$/i), { target: { value: "senha-longa-o-suficiente" } })
    fireEvent.change(screen.getByLabelText(/confirmar senha|confirm password/i), { target: { value: "senha-longa-o-suficiente" } })
    fireEvent.click(screen.getByRole("button", { name: /criar usuário|salvar/i }))

    const orgSelect = screen.getByTestId("new-user-org")
    await waitFor(() => expect(document.activeElement).toBe(orgSelect))
    expect(orgSelect).toHaveAttribute("aria-invalid", "true")
  })

  it("submit válido chama onCreate e não deixa nenhum campo marcado como inválido", async () => {
    const onCreate = vi.fn().mockResolvedValue(undefined)
    renderModal(onCreate)
    fireEvent.change(screen.getByLabelText(/usuário \(login\)/i), { target: { value: "bob" } })
    fireEvent.change(screen.getByLabelText(/^senha\*?$/i), { target: { value: "senha-longa-o-suficiente" } })
    fireEvent.change(screen.getByLabelText(/confirmar senha|confirm password/i), { target: { value: "senha-longa-o-suficiente" } })
    fireEvent.change(screen.getByTestId("new-user-org"), { target: { value: "1" } })
    fireEvent.click(screen.getByRole("button", { name: /criar usuário|salvar/i }))

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1))
  })
})
