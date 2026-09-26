import { fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { UserMenu } from "@/components/layout/UserMenu"
import { useAuth } from "@/contexts/AuthContext"
import i18n from "@/i18n"

vi.mock("@/contexts/AuthContext")

const mockedUseAuth = vi.mocked(useAuth)

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseAuth.mockReturnValue({
    user: {
      id: "1",
      username: "analyst",
      display_name: "Ana Lista",
      role: "admin",
      is_active: true,
      permissions: [] as string[],
    },
    loading: false,
    setupRequired: false,
    companyName: "ACME",
    companyPortalName: "Portal",
    ssoEnabled: false,
    ssoButtonLabel: "Entrar com Microsoft",
    login: vi.fn(),
    bootstrapAdmin: vi.fn(),
    logout: vi.fn(),
    updateUser: vi.fn(),
    refreshSession: vi.fn(),
    hasPermission: vi.fn(() => false),
  } as ReturnType<typeof useAuth>)
})

function renderMenu() {
  return render(
    <MemoryRouter>
      <UserMenu />
    </MemoryRouter>,
  )
}

describe("UserMenu — A11Y-12 (foco visível nos itens do menu)", () => {
  it("cada item do menu tem outline inset no focus-visible (não só focus:outline-none)", () => {
    renderMenu()
    fireEvent.click(screen.getByRole("button", { name: /Ana Lista/ }))

    const items = screen.getAllByRole("menuitem")
    expect(items.length).toBeGreaterThan(0)
    for (const item of items) {
      expect(item.className).toMatch(/focus-visible:outline/)
      expect(item.className).toMatch(/focus-visible:-outline-offset-2/)
    }
  })

  it("abre e fecha o menu ao clicar no gatilho", () => {
    renderMenu()
    const trigger = screen.getByRole("button", { name: /Ana Lista/ })
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()

    fireEvent.click(trigger)
    expect(screen.getByRole("menu")).toBeInTheDocument()

    fireEvent.click(trigger)
    expect(screen.queryByRole("menu")).not.toBeInTheDocument()
  })
})
