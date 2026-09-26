/**
 * EditUserModal — A11Y-26: erro acessível (aria-invalid/aria-describedby) e
 * foco no 1º campo inválido a cada tentativa de submit.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { EditUserModal } from "@/components/admin/EditUserModal"
import type { AppUser, Organization } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const ORGS: Organization[] = [
  { id: 1, name: "Acme", slug: "acme", is_active: true, integration_count: 0 },
]

const USER: AppUser = {
  id: "42",
  username: "alice",
  display_name: "Alice",
  role: "engineer",
  is_active: true,
  permissions: [],
  organization_id: null,
  organization_name: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
}

function renderModal(onSave = vi.fn().mockResolvedValue(undefined)) {
  render(<EditUserModal open user={USER} onClose={vi.fn()} onSave={onSave} organizations={ORGS} />)
  return { onSave }
}

describe("EditUserModal — A11Y-26", () => {
  it("senha curta: foca o campo de nova senha e liga aria-invalid/aria-describedby", async () => {
    renderModal()
    fireEvent.change(screen.getByLabelText(/^nova senha$/i), { target: { value: "curta" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }))

    const passwordInput = screen.getByLabelText(/^nova senha$/i)
    await waitFor(() => expect(document.activeElement).toBe(passwordInput))
    expect(passwordInput).toHaveAttribute("aria-invalid", "true")
    const describedBy = passwordInput.getAttribute("aria-describedby")!
    expect(describedBy).toContain("edit-user-modal-error")
    // A dica de tamanho mínimo continua ligada (não é substituída pelo erro).
    expect(describedBy).toContain("edit-user-new-password-helper")
  })

  it("senhas não coincidem: foca confirmar nova senha", async () => {
    renderModal()
    fireEvent.change(screen.getByLabelText(/^nova senha$/i), { target: { value: "senha-longa-o-suficiente" } })
    fireEvent.change(screen.getByLabelText(/confirmar nova senha/i), { target: { value: "outra-coisa" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }))

    const confirmInput = screen.getByLabelText(/confirmar nova senha/i)
    await waitFor(() => expect(document.activeElement).toBe(confirmInput))
    expect(confirmInput).toHaveAttribute("aria-invalid", "true")
  })

  it("role não-admin sem organização: foca o select de organização", async () => {
    renderModal()
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }))

    const orgSelect = screen.getByTestId("edit-user-org")
    await waitFor(() => expect(document.activeElement).toBe(orgSelect))
    expect(orgSelect).toHaveAttribute("aria-invalid", "true")
  })

  it("submit válido (sem trocar senha) chama onSave", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    renderModal(onSave)
    fireEvent.change(screen.getByTestId("edit-user-org"), { target: { value: "1" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar/i }))

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("42", expect.objectContaining({ organization_id: 1 })))
  })
})
