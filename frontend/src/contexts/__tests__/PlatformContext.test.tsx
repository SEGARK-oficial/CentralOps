/**
 * PlatformContext — a seleção de escopo pertence a UM usuário.
 *
 * Cobre o bug real: `selectedOrgId` nasce de `localStorage`, que sobrevive a
 * logout/login na mesma aba. Um admin que navega para a org 5, cria um operator
 * escopado à org 8 e loga como ele herdava "org 5" — e o dashboard negava
 * acesso à PRÓPRIA org do operator, porque o filtro enviado era de outra pessoa.
 *
 * A reconciliação é por IDENTIDADE do dono, não por conferência contra a lista
 * de organizações: a lista é paginada (`size=50`) e fica vazia quando o fetch
 * falha, e os dois casos apagariam uma seleção legítima.
 */

import type React from "react"
import { renderHook, waitFor } from "@testing-library/react"
import * as api from "@/services/api"
import { useAuth } from "../AuthContext"
import { PlatformProvider, usePlatform } from "../PlatformContext"
import type { AuthUser, Organization } from "@/types"

vi.mock("@/services/api")
vi.mock("../AuthContext")
const mockedApi = vi.mocked(api)
const mockedUseAuth = vi.mocked(useAuth)

function org(id: number, name = `org-${id}`): Organization {
  return { id, name, slug: name } as Organization
}

function asUser(id: string): AuthUser {
  return { id, username: `u${id}`, role: "operator", is_active: true, permissions: [] } as AuthUser
}

/** Loga um usuário específico para o próximo render. */
function login(id: string | null) {
  mockedUseAuth.mockReturnValue({
    user: id ? asUser(id) : null,
  } as ReturnType<typeof useAuth>)
}

const wrapper: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <PlatformProvider>{children}</PlatformProvider>
)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  mockedApi.listIntegrations.mockResolvedValue([])
  mockedApi.listOrganizations.mockResolvedValue([org(8)])
  login("1")
})

describe("PlatformContext — posse da seleção de escopo", () => {
  it("limpa a seleção herdada de OUTRO usuário", async () => {
    // Estado deixado pela sessão do admin (usuário 99) na mesma aba.
    localStorage.setItem("centralops_org_id", "5")
    localStorage.setItem("centralops_scope_owner", "99")
    login("1")

    const { result } = renderHook(() => usePlatform(), { wrapper })

    await waitFor(() => expect(result.current.selectedOrgId).toBeNull())
    expect(localStorage.getItem("centralops_org_id")).toBeNull()
    // A posse passa para quem está logado agora.
    expect(localStorage.getItem("centralops_scope_owner")).toBe("1")
  })

  it("preserva a seleção do PRÓPRIO usuário", async () => {
    localStorage.setItem("centralops_org_id", "8")
    localStorage.setItem("centralops_scope_owner", "1")
    login("1")

    const { result } = renderHook(() => usePlatform(), { wrapper })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.selectedOrgId).toBe(8)
  })

  it("limpa quando a marca de dono está ausente (build anterior)", async () => {
    // Sem a marca não dá para saber de quem é a seleção. Limpar custa uma
    // re-seleção; não limpar arrisca reproduzir o 403 que trava o dashboard.
    localStorage.setItem("centralops_org_id", "5")
    login("1")

    const { result } = renderHook(() => usePlatform(), { wrapper })

    await waitFor(() => expect(result.current.selectedOrgId).toBeNull())
    expect(localStorage.getItem("centralops_scope_owner")).toBe("1")
  })

  it("preserva a seleção quando o carregamento de organizações FALHA", async () => {
    // Falha de rede deixa `organizations` vazio, mas isso é ausência de
    // informação, não prova de que a org sumiu. Uma reconciliação baseada na
    // lista apagaria a seleção aqui; a baseada em dono não.
    localStorage.setItem("centralops_org_id", "8")
    localStorage.setItem("centralops_scope_owner", "1")
    login("1")
    mockedApi.listOrganizations.mockRejectedValue(new Error("backend fora"))

    const { result } = renderHook(() => usePlatform(), { wrapper })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.error).toBeTruthy()
    expect(result.current.selectedOrgId).toBe(8)
  })

  it("preserva a seleção de org fora da primeira página da lista", async () => {
    // `listOrganizations()` é chamado sem paginação e o backend devolve no
    // máximo 50. Num MSP, a org 777 pode ser válida e não estar na lista —
    // conferir contra ela apagaria uma seleção perfeitamente boa.
    localStorage.setItem("centralops_org_id", "777")
    localStorage.setItem("centralops_scope_owner", "1")
    login("1")
    mockedApi.listOrganizations.mockResolvedValue([org(1), org(2), org(3)])

    const { result } = renderHook(() => usePlatform(), { wrapper })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.selectedOrgId).toBe(777)
  })

  it("não mexe em nada antes de haver usuário autenticado", async () => {
    localStorage.setItem("centralops_org_id", "8")
    localStorage.setItem("centralops_scope_owner", "1")
    login(null)

    const { result } = renderHook(() => usePlatform(), { wrapper })

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.selectedOrgId).toBe(8)
    expect(localStorage.getItem("centralops_scope_owner")).toBe("1")
  })
})
