/**
 * AccountSettingsPage (self-service). Cobre: render do resumo read-only + formas,
 * o gate SSO (sem troca de senha/e-mail), a dirtiness do Save, a validação local
 * de senha (confirmação/força) e os caminhos felizes que chamam a API.
 *
 * i18n é forçado a pt (idioma padrão do produto); asserts usam data-testid/ids e
 * texto pt para não acoplar ao locale detectado no runner.
 */
import { render, screen, waitFor, fireEvent } from "@testing-library/react"
import { beforeAll, beforeEach, describe, it, expect, vi } from "vitest"

import AccountSettingsPage from "@/pages/AccountSettingsPage"
import * as api from "@/services/api"
import type { AccountProfile } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// updateUser vem do AuthContext; mockamos o hook para isolar a página.
const updateUser = vi.fn()
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ updateUser }),
}))

const LOCAL_USER: AccountProfile = {
  id: "u-1",
  username: "alice",
  email: "alice@corp.example",
  display_name: "Alice",
  auth_provider: "local",
  is_global: false,
  organization_id: 3,
  organization_name: "Acme",
  role: "operator",
  is_active: true,
  locale: "pt",
  permissions: [],
  created_at: "2026-01-01T10:00:00Z",
  last_login_at: "2026-07-01T09:00:00Z",
}

const SSO_USER: AccountProfile = {
  ...LOCAL_USER,
  username: "bob",
  email: "bob@corp.example",
  display_name: "Bob",
  auth_provider: "entra",
}

beforeEach(() => {
  vi.clearAllMocks()
})

function q(id: string): HTMLInputElement {
  const el = document.getElementById(id) as HTMLInputElement | null
  if (!el) throw new Error(`missing #${id}`)
  return el
}

describe("AccountSettingsPage", () => {
  it("renders the read-only summary and the password form for a local user", async () => {
    mockedApi.getMyProfile.mockResolvedValue(LOCAL_USER)
    render(<AccountSettingsPage />)

    await waitFor(() => expect(screen.getByTestId("account-page")).toBeTruthy())
    expect(screen.getByText("alice")).toBeTruthy()
    expect(screen.getByText("Acme")).toBeTruthy()
    // local sign-in method label (pt) + password form present
    expect(screen.getByText("Senha local")).toBeTruthy()
    expect(screen.getByTestId("account-change-password")).toBeTruthy()
    // Save disabled while pristine
    expect(screen.getByTestId("account-save-profile")).toHaveProperty("disabled", true)
  })

  it("hides password change and disables email for an SSO user", async () => {
    mockedApi.getMyProfile.mockResolvedValue(SSO_USER)
    render(<AccountSettingsPage />)

    await waitFor(() => expect(screen.getByTestId("account-page")).toBeTruthy())
    expect(screen.queryByTestId("account-change-password")).toBeNull()
    expect(q("account-email").disabled).toBe(true)
    // sign-out others is still available for SSO users
    expect(screen.getByTestId("account-signout-others")).toBeTruthy()
  })

  it("saves display_name changes via updateMyProfile and refreshes the header", async () => {
    mockedApi.getMyProfile.mockResolvedValue(LOCAL_USER)
    mockedApi.updateMyProfile.mockResolvedValue({ ...LOCAL_USER, display_name: "Alice Cooper" })
    render(<AccountSettingsPage />)
    await waitFor(() => expect(screen.getByTestId("account-page")).toBeTruthy())

    fireEvent.change(q("account-display-name"), { target: { value: "Alice Cooper" } })
    const save = screen.getByTestId("account-save-profile") as HTMLButtonElement
    expect(save.disabled).toBe(false)
    fireEvent.click(save)

    await waitFor(() => expect(mockedApi.updateMyProfile).toHaveBeenCalledWith({ display_name: "Alice Cooper" }))
    expect(updateUser).toHaveBeenCalledWith(
      expect.objectContaining({ display_name: "Alice Cooper" }),
    )
  })

  it("requires the current password when changing email", async () => {
    mockedApi.getMyProfile.mockResolvedValue(LOCAL_USER)
    render(<AccountSettingsPage />)
    await waitFor(() => expect(screen.getByTestId("account-page")).toBeTruthy())

    fireEvent.change(q("account-email"), { target: { value: "alice.new@corp.example" } })
    // the confirm-password field appears once email is dirty
    await waitFor(() => expect(document.getElementById("account-email-password")).toBeTruthy())
    fireEvent.click(screen.getByTestId("account-save-profile"))

    // no current password → the page blocks the call and shows the guidance
    expect(mockedApi.updateMyProfile).not.toHaveBeenCalled()
    expect(screen.getByText(/informe sua senha atual/i)).toBeTruthy()
  })

  it("blocks a mismatched new-password confirmation without calling the API", async () => {
    mockedApi.getMyProfile.mockResolvedValue(LOCAL_USER)
    render(<AccountSettingsPage />)
    await waitFor(() => expect(screen.getByTestId("account-page")).toBeTruthy())

    fireEvent.change(q("account-current-password"), { target: { value: "CurrentPass123" } })
    fireEvent.change(q("account-new-password"), { target: { value: "BrandNewPass456" } })
    fireEvent.change(q("account-confirm-password"), { target: { value: "different-999" } })
    fireEvent.click(screen.getByTestId("account-change-password"))

    expect(mockedApi.changeMyPassword).not.toHaveBeenCalled()
    expect(screen.getByText(/não coincidem/i)).toBeTruthy()
  })

  it("changes the password and reports revoked sessions", async () => {
    mockedApi.getMyProfile.mockResolvedValue(LOCAL_USER)
    mockedApi.changeMyPassword.mockResolvedValue({ detail: "password_changed", revoked_other_sessions: 2 })
    render(<AccountSettingsPage />)
    await waitFor(() => expect(screen.getByTestId("account-page")).toBeTruthy())

    fireEvent.change(q("account-current-password"), { target: { value: "CurrentPass123" } })
    fireEvent.change(q("account-new-password"), { target: { value: "BrandNewPass456" } })
    fireEvent.change(q("account-confirm-password"), { target: { value: "BrandNewPass456" } })
    fireEvent.click(screen.getByTestId("account-change-password"))

    await waitFor(() =>
      expect(mockedApi.changeMyPassword).toHaveBeenCalledWith({
        current_password: "CurrentPass123",
        new_password: "BrandNewPass456",
      }),
    )
    // success feedback mentions the 2 revoked sessions
    await waitFor(() => expect(screen.getByText(/2 outra/i)).toBeTruthy())
  })
})
