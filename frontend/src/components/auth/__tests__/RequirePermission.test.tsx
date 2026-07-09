import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { RequirePermission } from "@/components/auth/RequirePermission"
import { usePermission } from "@/hooks/usePermission"

vi.mock("@/hooks/usePermission")
const mockedUsePermission = vi.mocked(usePermission)

function renderWithRouter(ui: React.ReactElement) {
  return render(<MemoryRouter>{ui}</MemoryRouter>)
}

describe("RequirePermission", () => {
  it("renderiza children quando usuário tem a permissão", () => {
    mockedUsePermission.mockReturnValue(true)
    renderWithRouter(
      <RequirePermission perm="user.manage">
        <div>Conteúdo protegido</div>
      </RequirePermission>,
    )
    expect(screen.getByText("Conteúdo protegido")).toBeInTheDocument()
    expect(mockedUsePermission).toHaveBeenCalledWith("user.manage")
  })

  it("redireciona para / quando usuário não tem a permissão (sem fallback)", () => {
    mockedUsePermission.mockReturnValue(false)
    renderWithRouter(
      <RequirePermission perm="user.manage">
        <div>Conteúdo protegido</div>
      </RequirePermission>,
    )
    expect(screen.queryByText("Conteúdo protegido")).not.toBeInTheDocument()
  })

  it("renderiza fallback quando usuário não tem a permissão", () => {
    mockedUsePermission.mockReturnValue(false)
    renderWithRouter(
      <RequirePermission perm="user.manage" fallback={<div>Acesso negado</div>}>
        <div>Conteúdo protegido</div>
      </RequirePermission>,
    )
    expect(screen.getByText("Acesso negado")).toBeInTheDocument()
    expect(screen.queryByText("Conteúdo protegido")).not.toBeInTheDocument()
  })

  it("não renderiza fallback quando permissão está presente", () => {
    mockedUsePermission.mockReturnValue(true)
    renderWithRouter(
      <RequirePermission perm="mapping.write" fallback={<div>Acesso negado</div>}>
        <div>Editor de mapping</div>
      </RequirePermission>,
    )
    expect(screen.getByText("Editor de mapping")).toBeInTheDocument()
    expect(screen.queryByText("Acesso negado")).not.toBeInTheDocument()
  })
})
