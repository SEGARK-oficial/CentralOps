import { render, screen, waitFor } from "@testing-library/react"
import { RolePermissionsViewer } from "@/components/admin/RolePermissionsViewer"
import { usePermissionsMatrix } from "@/hooks/usePermissionsMatrix"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/usePermissionsMatrix")
const mockedHook = vi.mocked(usePermissionsMatrix)

const fakeMatrix = {
  viewer: ["mapping.read"],
  operator: ["mapping.read", "drift.ignore"],
  engineer: ["mapping.read", "mapping.write"],
  admin: ["mapping.read", "mapping.write", "user.manage"],
}

describe("RolePermissionsViewer", () => {
  it("não renderiza quando fechado", () => {
    mockedHook.mockReturnValue({ matrix: fakeMatrix, isLoading: false, error: null })
    render(<RolePermissionsViewer open={false} onClose={vi.fn()} />)
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("mostra loading quando isLoading=true", () => {
    mockedHook.mockReturnValue({ matrix: null, isLoading: true, error: null })
    render(<RolePermissionsViewer open={true} onClose={vi.fn()} />)
    expect(screen.getByRole("dialog")).toBeInTheDocument()
    expect(screen.getByText(/carregando/i)).toBeInTheDocument()
  })

  it("mostra erro quando hook retorna erro", () => {
    mockedHook.mockReturnValue({
      matrix: null,
      isLoading: false,
      error: new Error("Falha de rede"),
    })
    render(<RolePermissionsViewer open={true} onClose={vi.fn()} />)
    expect(screen.getByText("Falha de rede")).toBeInTheDocument()
  })

  it("renderiza tabela com papéis e permissões", async () => {
    mockedHook.mockReturnValue({ matrix: fakeMatrix, isLoading: false, error: null })
    render(<RolePermissionsViewer open={true} onClose={vi.fn()} />)

    await waitFor(() => {
      expect(screen.getByRole("table")).toBeInTheDocument()
    })

    // Colunas de role
    expect(screen.getByText("viewer")).toBeInTheDocument()
    expect(screen.getByText("admin")).toBeInTheDocument()

    // Permissões na tabela
    expect(screen.getByText("mapping.read")).toBeInTheDocument()
    expect(screen.getByText("user.manage")).toBeInTheDocument()
  })
})
