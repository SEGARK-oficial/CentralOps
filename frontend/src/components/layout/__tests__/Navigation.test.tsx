import { render, screen, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { Navigation } from "@/components/layout/Navigation"
import { useAuth } from "@/contexts/AuthContext"
import { usePermission } from "@/hooks/usePermission"
import i18n from "@/i18n"

vi.mock("@/contexts/AuthContext")
vi.mock("@/hooks/usePermission")

const mockedUseAuth = vi.mocked(useAuth)
const mockedUsePermission = vi.mocked(usePermission)

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

function makeUser(role: "admin" | "user" = "user") {
  return {
    id: "1",
    username: "test",
    display_name: "Test",
    role,
    is_active: true,
    permissions: [] as string[],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseAuth.mockReturnValue({
    user: makeUser(),
    loading: false,
    setupRequired: false,
    companyName: "ACME",
    companyPortalName: "Portal",
    ssoEnabled: false,
    ssoButtonLabel: "Entrar com Microsoft",
    login: vi.fn(),
    bootstrapAdmin: vi.fn(),
    logout: vi.fn(),
    refreshSession: vi.fn(),
    hasPermission: vi.fn(() => false),
  } as ReturnType<typeof useAuth>)
  mockedUsePermission.mockReturnValue(false)
})

function renderNav(open = false, onClose = vi.fn()) {
  return render(
    <MemoryRouter>
      <Navigation open={open} onClose={onClose} />
    </MemoryRouter>,
  )
}

describe("Navigation — drawer", () => {
  it("está fora da tela quando open=false (translate-x negativo)", () => {
    renderNav(false)
    const nav = screen.getByRole("navigation")
    expect(nav.className).toMatch(/-translate-x-full/)
  })

  it("está visível quando open=true (sem translate negativo)", () => {
    renderNav(true)
    const nav = screen.getByRole("dialog")
    expect(nav.className).not.toMatch(/-translate-x-full/)
    expect(nav.className).toMatch(/translate-x-0/)
  })

  it("role=dialog quando open=true, role=navigation quando open=false", () => {
    const { rerender } = renderNav(false)
    expect(screen.getByRole("navigation")).toBeInTheDocument()

    rerender(
      <MemoryRouter>
        <Navigation open={true} onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.getByRole("dialog")).toBeInTheDocument()
  })

  it("chama onClose ao pressionar ESC quando open=true", () => {
    const onClose = vi.fn()
    renderNav(true, onClose)

    fireEvent.keyDown(document, { key: "Escape" })

    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("não chama onClose ao pressionar ESC quando open=false", () => {
    const onClose = vi.fn()
    renderNav(false, onClose)

    fireEvent.keyDown(document, { key: "Escape" })

    expect(onClose).not.toHaveBeenCalled()
  })

  it("chama onClose ao clicar em um NavLink (fechar ao navegar)", () => {
    const onClose = vi.fn()
    renderNav(true, onClose)

    const dashboardLink = screen.getByText("Dashboard").closest("a")!
    fireEvent.click(dashboardLink)

    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe("Navigation — rail colapsável (desktop)", () => {
  it("aplica largura de rail (lg:w-16) quando collapsed=true", () => {
    render(
      <MemoryRouter>
        <Navigation collapsed onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.getByRole("navigation").className).toMatch(/lg:w-16/)
  })

  it("aplica largura plena (lg:w-56) quando não colapsada", () => {
    render(
      <MemoryRouter>
        <Navigation onClose={vi.fn()} />
      </MemoryRouter>,
    )
    expect(screen.getByRole("navigation").className).toMatch(/lg:w-56/)
  })

  it("oculta os rótulos no rail (lg:hidden) quando collapsed=true", () => {
    render(
      <MemoryRouter>
        <Navigation collapsed onClose={vi.fn()} />
      </MemoryRouter>,
    )
    // O texto do label permanece no DOM (acessível via aria-label) mas é oculto em lg.
    const label = screen.getByText("Dashboard")
    expect(label.className).toMatch(/lg:hidden/)
  })
})
