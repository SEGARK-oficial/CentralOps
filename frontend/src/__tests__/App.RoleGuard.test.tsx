/**
 * R3-9.1 — `RoleGuard` (App.tsx): a dependência do `useEffect` de checagem de
 * admin usava `[role, user?.id, user?.role]` mas o CORPO acessava o objeto
 * `user` inteiro (`!user`) — violação real de `react-hooks/exhaustive-deps`,
 * não um falso positivo. O fix reescreve a guarda para só tocar
 * `user?.id`/`user?.role` (já na dependency list).
 *
 * Teste do caminho feliz E do caminho de "corrida"/estabilidade: uma
 * atualização de PERFIL que preserva id/role (o padrão de `updateUser`, que
 * faz merge parcial e troca a identidade do objeto `user`) NÃO deve
 * redisparar `verifyAdminAccess` — o comportamento que a dependency list
 * estreita já garantia antes do fix, e que uma "correção" ingênua (depender
 * do objeto inteiro) teria QUEBRADO.
 */
import { act } from "react"
import { render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { RoleGuard } from "@/App"
import { useAuth } from "@/contexts/AuthContext"
import * as api from "@/services/api"
import type { AuthUser } from "@/types"

vi.mock("@/contexts/AuthContext")
vi.mock("@/services/api")

const mockedUseAuth = vi.mocked(useAuth)
const mockedApi = vi.mocked(api)

function makeAdminUser(overrides: Partial<AuthUser> = {}): AuthUser {
  return {
    id: "1",
    username: "admin",
    display_name: "Admin",
    role: "admin",
    is_active: true,
    permissions: [],
    ...overrides,
  } as AuthUser
}

function mockAuth(user: AuthUser | null) {
  mockedUseAuth.mockReturnValue({
    user,
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
}

function renderGuard() {
  return render(
    <MemoryRouter>
      <RoleGuard role="admin">
        <div data-testid="protected">conteúdo protegido</div>
      </RoleGuard>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("RoleGuard — caminho feliz", () => {
  it("admin com verifyAdminAccess OK: renderiza os filhos", async () => {
    mockAuth(makeAdminUser())
    mockedApi.verifyAdminAccess.mockResolvedValue(undefined as never)

    renderGuard()

    await waitFor(() => expect(screen.getByTestId("protected")).toBeInTheDocument())
    expect(mockedApi.verifyAdminAccess).toHaveBeenCalledTimes(1)
  })

  it("não-admin: nunca chama verifyAdminAccess e redireciona (não renderiza os filhos)", async () => {
    mockAuth(makeAdminUser({ role: "viewer" }))

    renderGuard()

    await waitFor(() => expect(screen.queryByTestId("protected")).not.toBeInTheDocument())
    expect(mockedApi.verifyAdminAccess).not.toHaveBeenCalled()
  })
})

describe("RoleGuard — estabilidade da dependência (não é loop, não perde re-render)", () => {
  it("uma atualização de perfil que preserva id/role (identidade de `user` troca, valores não) NÃO rechama verifyAdminAccess", async () => {
    mockedApi.verifyAdminAccess.mockResolvedValue(undefined as never)
    const admin = makeAdminUser({ display_name: "Admin Original" })
    mockAuth(admin)

    const { rerender } = renderGuard()
    await waitFor(() => expect(mockedApi.verifyAdminAccess).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.getByTestId("protected")).toBeInTheDocument())

    // Simula o que `updateUser` faz de verdade: merge parcial → NOVO objeto,
    // MESMOS id/role. Se o efeito dependesse do objeto `user` inteiro (em vez
    // de só id/role), isto dispararia uma 2ª chamada desnecessária.
    mockAuth({ ...admin, display_name: "Admin Renomeado" })
    act(() => {
      rerender(
        <MemoryRouter>
          <RoleGuard role="admin">
            <div data-testid="protected">conteúdo protegido</div>
          </RoleGuard>
        </MemoryRouter>,
      )
    })

    expect(screen.getByTestId("protected")).toBeInTheDocument()
    expect(mockedApi.verifyAdminAccess).toHaveBeenCalledTimes(1)
  })

  it("uma troca real de role (perde admin) RE-executa a checagem e redireciona", async () => {
    mockedApi.verifyAdminAccess.mockResolvedValue(undefined as never)
    const admin = makeAdminUser()
    mockAuth(admin)

    const { rerender } = renderGuard()
    await waitFor(() => expect(screen.getByTestId("protected")).toBeInTheDocument())

    mockAuth({ ...admin, role: "viewer" })
    act(() => {
      rerender(
        <MemoryRouter>
          <RoleGuard role="admin">
            <div data-testid="protected">conteúdo protegido</div>
          </RoleGuard>
        </MemoryRouter>,
      )
    })

    await waitFor(() => expect(screen.queryByTestId("protected")).not.toBeInTheDocument())
    // A chamada de rede não se repete (o guard cai em "idle" sem chamar a API
    // de novo) — continua tendo sido chamada só na 1ª vez, quando era admin.
    expect(mockedApi.verifyAdminAccess).toHaveBeenCalledTimes(1)
  })
})
