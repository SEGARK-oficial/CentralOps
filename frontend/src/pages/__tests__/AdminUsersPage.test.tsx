import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import AdminUsersPage from "@/pages/AdminUsersPage"
import { useAuth } from "@/contexts/AuthContext"
import { useUsers } from "@/hooks/useUsers"
import { usePermission } from "@/hooks/usePermission"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/contexts/AuthContext")
vi.mock("@/hooks/useUsers")
vi.mock("@/hooks/usePermission")
// Evita que RolePermissionsViewer tente chamar a API real
vi.mock("@/hooks/usePermissionsMatrix", () => ({
  usePermissionsMatrix: () => ({ matrix: null, isLoading: false, error: null }),
}))

const mockedUseAuth = vi.mocked(useAuth)
const mockedUseUsers = vi.mocked(useUsers)
const mockedUsePermission = vi.mocked(usePermission)

const adminUser = {
  id: "1",
  username: "admin",
  role: "admin" as const,
  is_active: true,
  permissions: ["user.manage"],
}

const fakeUsers = [
  {
    id: "10",
    username: "alice",
    display_name: "Alice",
    role: "engineer" as const,
    is_active: true,
    permissions: ["mapping.write"],
    organization_id: null,
    organization_name: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_login_at: null,
  },
  {
    id: "11",
    username: "bob",
    display_name: "Bob",
    role: "viewer" as const,
    is_active: false,
    permissions: [],
    organization_id: null,
    organization_name: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    last_login_at: null,
  },
]

const mockUseUsers = (overrides = {}) => ({
  users: fakeUsers,
  isLoading: false,
  error: null,
  refetch: vi.fn(),
  createUser: vi.fn().mockResolvedValue(fakeUsers[0]),
  updateUser: vi.fn().mockResolvedValue(fakeUsers[0]),
  deleteUser: vi.fn().mockResolvedValue(undefined),
  ...overrides,
})

function renderPage() {
  return render(
    <MemoryRouter>
      <AdminUsersPage />
    </MemoryRouter>,
  )
}

describe("AdminUsersPage", () => {
  beforeEach(() => {
    mockedUseAuth.mockReturnValue({
      user: adminUser,
      refreshSession: vi.fn(),
    } as never)
    mockedUseUsers.mockReturnValue(mockUseUsers())
    // Por padrão admin tem user.manage
    mockedUsePermission.mockImplementation((perm: string) => perm === "user.manage")
  })

  it("renderiza a página com data-testid correto", () => {
    renderPage()
    expect(screen.getByTestId("admin-users-page")).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Usuários" })).toBeInTheDocument()
  })

  it("renderiza tabela de usuários", () => {
    renderPage()
    expect(screen.getByTestId("users-table")).toBeInTheDocument()
    expect(screen.getByText("Alice")).toBeInTheDocument()
    expect(screen.getByText("Bob")).toBeInTheDocument()
  })

  it("mostra botão Novo usuário quando tem user.manage", () => {
    renderPage()
    expect(screen.getByTestId("new-user-button")).toBeInTheDocument()
  })

  it("esconde botão Novo usuário quando não tem user.manage", () => {
    mockedUsePermission.mockReturnValue(false)
    renderPage()
    expect(screen.queryByTestId("new-user-button")).not.toBeInTheDocument()
  })

  it("mostra loading spinner quando isLoading=true", () => {
    mockedUseUsers.mockReturnValue(mockUseUsers({ isLoading: true, users: [] }))
    renderPage()
    expect(screen.getByText(/carregando usuários/i)).toBeInTheDocument()
  })

  it("mostra empty state quando não há usuários", () => {
    mockedUseUsers.mockReturnValue(mockUseUsers({ users: [] }))
    renderPage()
    expect(screen.getByText(/nenhum usuário cadastrado/i)).toBeInTheDocument()
  })

  it("abre modal de novo usuário ao clicar no botão", () => {
    renderPage()
    fireEvent.click(screen.getByTestId("new-user-button"))
    const dialog = screen.getByRole("dialog")
    expect(dialog).toBeInTheDocument()
    // Dentro do dialog deve haver o título "Novo usuário"
    expect(within(dialog).getByText("Novo usuário")).toBeInTheDocument()
  })

  it("abre modal de editar papel ao clicar em Editar papel", () => {
    renderPage()
    fireEvent.click(screen.getByTestId(`edit-role-${fakeUsers[0].id}`))
    const dialog = screen.getByRole("dialog")
    expect(dialog).toBeInTheDocument()
    expect(within(dialog).getByText("Editar papel do usuário")).toBeInTheDocument()
  })

  it("chama updateUser ao mudar role e salvar", async () => {
    const updateUser = vi.fn().mockResolvedValue(fakeUsers[0])
    mockedUseUsers.mockReturnValue(mockUseUsers({ updateUser }))

    renderPage()
    fireEvent.click(screen.getByTestId(`edit-role-${fakeUsers[0].id}`))

    // Muda para admin
    const select = screen.getByTestId("role-select")
    fireEvent.change(select, { target: { value: "admin" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar papel/i }))

    await waitFor(() => {
      expect(updateUser).toHaveBeenCalledWith(fakeUsers[0].id, { role: "admin" })
    })
  })

  it("mostra ConfirmDialog ao clicar em Excluir", () => {
    renderPage()
    fireEvent.click(screen.getByTestId(`delete-user-${fakeUsers[0].id}`))
    const dialog = screen.getByRole("dialog")
    expect(dialog).toBeInTheDocument()
    expect(within(dialog).getByRole("heading", { name: "Excluir usuário" })).toBeInTheDocument()
  })

  it("chama deleteUser ao confirmar exclusão", async () => {
    const deleteUser = vi.fn().mockResolvedValue(undefined)
    mockedUseUsers.mockReturnValue(mockUseUsers({ deleteUser }))

    renderPage()
    fireEvent.click(screen.getByTestId(`delete-user-${fakeUsers[0].id}`))
    fireEvent.click(screen.getByRole("button", { name: /^excluir usuário$/i }))

    await waitFor(() => {
      expect(deleteUser).toHaveBeenCalledWith(fakeUsers[0].id)
    })
  })

  it("mostra erro quando useUsers retorna erro", () => {
    mockedUseUsers.mockReturnValue(mockUseUsers({ error: new Error("API indisponível"), users: [] }))
    renderPage()
    expect(screen.getByText("API indisponível")).toBeInTheDocument()
  })
})
