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

function renderNav(open = false, onClose = vi.fn(), path = "/dashboard") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Navigation open={open} onClose={onClose} />
    </MemoryRouter>,
  )
}

/** Bloco do grupo: o <div> que embrulha barra de estágio + rótulo + itens. */
function groupBlock(label: string): HTMLElement {
  return screen.getByText(label).parentElement as HTMLElement
}

/**
 * Barra de estágio do grupo, ou null num grupo neutro. Filha DIRETA do bloco —
 * senão o seletor pega o span do ícone do primeiro item.
 */
function stageBar(label: string): HTMLElement | null {
  return groupBlock(label).querySelector<HTMLElement>(':scope > span[aria-hidden="true"]')
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

describe("Navigation — grupos por estágio do pipeline", () => {
  it("ordena os grupos pelo pipeline, com o bloco neutro depois", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    mockedUsePermission.mockReturnValue(true)

    const { container } = renderNav()
    const labels = Array.from(container.querySelectorAll(".font-mono")).map((el) => el.textContent)

    // A âncora (visão geral) não tem rótulo mono: é o topo do rail, não um estágio.
    expect(labels).toEqual(["Coleta", "Normaliza", "Roteia", "Detecta", "Administração"])
  })

  it("dá a cada grupo de estágio a matiz do seu estágio", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    mockedUsePermission.mockReturnValue(true)

    renderNav()

    expect(stageBar("Coleta")?.className).toMatch(/bg-stage-collect/)
    expect(stageBar("Normaliza")?.className).toMatch(/bg-stage-normalize/)
    expect(stageBar("Roteia")?.className).toMatch(/bg-stage-route/)
    expect(stageBar("Detecta")?.className).toMatch(/bg-stage-detect/)
  })

  it("não põe barra de estágio no bloco neutro", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    mockedUsePermission.mockReturnValue(true)

    renderNav()

    expect(stageBar("Administração")).toBeNull()
  })

  it("acende a barra do estágio onde a rota atual está", () => {
    renderNav(false, vi.fn(), "/mappings")

    expect(stageBar("Normaliza")?.className).toMatch(/opacity-100/)
    expect(stageBar("Coleta")?.className).toMatch(/opacity-40/)
  })

  it("omite o grupo Reduz enquanto não houver tela de redução", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    mockedUsePermission.mockReturnValue(true)

    renderNav()

    expect(screen.queryByText("Reduz")).toBeNull()
  })

  it("esconde Roteia e Administração de quem não é admin", () => {
    renderNav()

    expect(screen.queryByText("Roteia")).toBeNull()
    expect(screen.queryByText("Administração")).toBeNull()
    expect(screen.getByText("Coleta")).toBeInTheDocument()
  })
})

describe("Navigation — âncora fixa no topo", () => {
  /** O container que rola: tudo que estiver dentro dele pode sair da dobra. */
  const scrollRegion = (container: HTMLElement) =>
    container.querySelector<HTMLElement>(".overflow-y-auto")!

  it("mantém Dashboard, Saúde do pipeline e Histórico FORA da área que rola", () => {
    const { container } = renderNav()
    const scroll = scrollRegion(container)

    for (const label of ["Dashboard", "Saúde do pipeline", "Histórico"]) {
      const link = screen.getByText(label).closest("a")!
      expect(scroll.contains(link)).toBe(false)
    }
  })

  it("põe a âncora ANTES do primeiro estágio na ordem do DOM", () => {
    const { container } = renderNav()
    const order = Array.from(container.querySelectorAll("a")).map((a) => a.textContent)

    expect(order.slice(0, 3)).toEqual(["Dashboard", "Saúde do pipeline", "Histórico"])
  })

  it("nomeia a lista da âncora para leitor de tela (não há rótulo visível)", () => {
    renderNav()

    expect(screen.getByLabelText("Visão geral")).toBeInTheDocument()
  })

  it("deixa os estágios e a administração na área que rola", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    mockedUsePermission.mockReturnValue(true)

    const { container } = renderNav()
    const scroll = scrollRegion(container)

    expect(scroll.contains(screen.getByText("Integrações").closest("a"))).toBe(true)
    expect(scroll.contains(screen.getByText("Organizações").closest("a"))).toBe(true)
  })

  it("não repete Minha conta e Tokens no rail — quem cobre é o UserMenu", () => {
    mockedUseAuth.mockReturnValue({
      ...mockedUseAuth(),
      user: makeUser("admin"),
    } as ReturnType<typeof useAuth>)
    mockedUsePermission.mockReturnValue(true)

    renderNav()

    expect(screen.queryByText("Conta")).toBeNull()
    expect(screen.queryByRole("link", { name: "Perfil e segurança" })).toBeNull()
    expect(screen.queryByRole("link", { name: "Tokens de API" })).toBeNull()
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
