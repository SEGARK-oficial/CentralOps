import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { GlobalFilters } from "@/components/layout/GlobalFilters"
import { usePlatform } from "@/contexts/PlatformContext"
import i18n from "@/i18n"

vi.mock("@/contexts/PlatformContext")

const mockedUsePlatform = vi.mocked(usePlatform)

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

function basePlatform(overrides: Partial<ReturnType<typeof usePlatform>> = {}): ReturnType<typeof usePlatform> {
  return {
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
    ...overrides,
  } as ReturnType<typeof usePlatform>
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <GlobalFilters />
    </MemoryRouter>,
  )
}

describe("GlobalFilters — LAY-09 (skeleton em vez de CLS)", () => {
  it("mostra um skeleton com role=status durante o loading, em vez de desaparecer", () => {
    mockedUsePlatform.mockReturnValue(basePlatform({ loading: true }))
    renderAt("/dashboard")

    expect(screen.getByRole("status")).toBeInTheDocument()
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument()
  })

  it("renderiza os selects reais quando loading termina", () => {
    mockedUsePlatform.mockReturnValue(basePlatform({ loading: false }))
    renderAt("/dashboard")

    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })

  it("continua oculta em rotas de administração, mesmo carregando", () => {
    mockedUsePlatform.mockReturnValue(basePlatform({ loading: true }))
    renderAt("/config")

    expect(screen.queryByRole("status")).not.toBeInTheDocument()
  })
})
