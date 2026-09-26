import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { Link, MemoryRouter, Route, Routes } from "react-router-dom"
import { AppLayout } from "@/components/layout/AppLayout"
import { useAuth } from "@/contexts/AuthContext"
import { usePlatform } from "@/contexts/PlatformContext"
import { usePermission } from "@/hooks/usePermission"
import i18n from "@/i18n"

vi.mock("@/contexts/AuthContext")
vi.mock("@/contexts/PlatformContext")
vi.mock("@/hooks/usePermission")

const mockedUseAuth = vi.mocked(useAuth)
const mockedUsePlatform = vi.mocked(usePlatform)
const mockedUsePermission = vi.mocked(usePermission)

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseAuth.mockReturnValue({
    user: {
      id: "1",
      username: "test",
      display_name: "Test",
      role: "user",
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
    refreshSession: vi.fn(),
    hasPermission: vi.fn(() => false),
  } as ReturnType<typeof useAuth>)
  mockedUsePermission.mockReturnValue(false)
  mockedUsePlatform.mockReturnValue({
    organizations: [],
    integrations: [],
    filteredIntegrations: [],
    loading: false,
    error: null,
    selectedOrgId: null,
    selectedPlatform: null,
    selectedIntegrationId: null,
    setSelectedOrgId: vi.fn(),
    setSelectedPlatform: vi.fn(),
    setSelectedIntegrationId: vi.fn(),
    selectedOrganization: null,
    selectedIntegration: null,
    refreshData: vi.fn(),
    clearFilters: vi.fn(),
  } as ReturnType<typeof usePlatform>)
})

afterEach(() => {
  document.title = ""
})

function PageA() {
  return (
    <>
      <h1>Página A</h1>
      <Link to="/b">Ir para B</Link>
    </>
  )
}
function PageB() {
  return <h1>Página B</h1>
}

function renderShell(initialPath = "/a") {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route path="/a" element={<PageA />} />
          <Route path="/b" element={<PageB />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe("AppLayout — A11Y-07 (título por rota)", () => {
  it("define document.title a partir do <h1> da página renderizada", async () => {
    renderShell("/a")
    await waitFor(() => expect(document.title).toBe("Página A — CentralOps"))
  })
})

describe("AppLayout — A11Y-08 (foco ao trocar de rota)", () => {
  it("NÃO rouba o foco no carregamento inicial", async () => {
    renderShell("/a")
    await screen.findByText("Página A")
    const main = screen.getByRole("main")
    expect(main).not.toHaveFocus()
  })

  it("main tem tabIndex=-1 para poder receber foco programático", async () => {
    renderShell("/a")
    await screen.findByText("Página A")
    expect(screen.getByRole("main")).toHaveAttribute("tabindex", "-1")
  })

  it("move o foco para o <main> depois de uma navegação real (não no mount)", async () => {
    renderShell("/a")
    await screen.findByText("Página A")
    expect(screen.getByRole("main")).not.toHaveFocus()

    fireEvent.click(screen.getByRole("link", { name: "Ir para B" }))

    await screen.findByText("Página B")
    await waitFor(() => expect(screen.getByRole("main")).toHaveFocus())
    expect(document.title).toBe("Página B — CentralOps")
  })
})

describe("AppLayout — LAY-08/LAY-41 (shell)", () => {
  it("usa h-dvh no container raiz (evita 100vh com teclado virtual mobile)", () => {
    const { container } = renderShell("/a")
    expect(container.querySelector(".flex.h-dvh")).toBeInTheDocument()
  })
})
