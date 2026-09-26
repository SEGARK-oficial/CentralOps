import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { Header } from "@/components/layout/Header"
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
    companyName: "ACME Corp",
    companyPortalName: "Portal",
    ssoEnabled: false,
    ssoButtonLabel: "Entrar com Microsoft",
    login: vi.fn(),
    bootstrapAdmin: vi.fn(),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    hasPermission: vi.fn(() => false),
  } as ReturnType<typeof useAuth>)
})

describe("Header — A11Y-33 (nome da instalação não é h1)", () => {
  it("mostra o nome da empresa sem usar <h1> (evita duplicar o h1 real da página)", () => {
    render(
      <MemoryRouter>
        <Header onToggleSidebar={vi.fn()} />
      </MemoryRouter>,
    )

    expect(screen.getByText("ACME Corp")).toBeInTheDocument()
    expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument()
  })
})
