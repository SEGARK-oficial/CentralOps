/**
 * AuthContext.logout — limpeza do escopo de organização cacheado.
 *
 * `PlatformContext` guarda a org selecionada em `localStorage` para
 * sobreviver a um F5, mas isso também a fazia sobreviver a uma TROCA DE
 * USUÁRIO na mesma aba: o próximo login herdava a org de quem saiu. Logout
 * é a defesa na origem — ver também `PlatformContext.test.tsx`, que cobre a
 * reconciliação (a defesa que fecha o buraco de verdade).
 */

import { renderHook, act } from "@testing-library/react"
import * as api from "@/services/api"
import { AuthProvider, useAuth } from "../AuthContext"
import type { AuthUser } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  mockedApi.getAuthStatus.mockResolvedValue({
    setup_required: false,
    company_name: "CentralOps",
    company_portal_name: "Portal de Login",
    sso_enabled: false,
    sso_button_label: null,
  })
  mockedApi.getCurrentUser.mockResolvedValue({
    id: "1", username: "admin", role: "admin", is_active: true, permissions: [],
  } as AuthUser)
})

describe("AuthContext.logout", () => {
  it("limpa o escopo de organização cacheado", async () => {
    localStorage.setItem("centralops_org_id", "5")
    localStorage.setItem("centralops_platform", "wazuh")
    localStorage.setItem("centralops_integration_id", "12")
    mockedApi.logout.mockResolvedValue(undefined)

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })
    await act(async () => {
      await result.current.logout()
    })

    expect(localStorage.getItem("centralops_org_id")).toBeNull()
    expect(localStorage.getItem("centralops_platform")).toBeNull()
    expect(localStorage.getItem("centralops_integration_id")).toBeNull()
  })

  it("limpa o escopo mesmo se a chamada de logout falhar", async () => {
    // A sessão já pode estar morta no servidor (cookie expirado). O estado
    // local tem que ser limpo de qualquer jeito — é o que já acontecia para
    // `setUser(null)`, e o cache de org precisa do mesmo tratamento.
    localStorage.setItem("centralops_org_id", "5")
    mockedApi.logout.mockRejectedValue(new Error("401"))

    const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider })
    await act(async () => {
      await result.current.logout()
    })

    expect(localStorage.getItem("centralops_org_id")).toBeNull()
  })
})
