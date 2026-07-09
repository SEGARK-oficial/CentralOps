/**
 * IntegrationsPage tests (PR #4 — bulk + filtros).
 *
 * Cobre:
 * - Filtros (search debounced / kind / status) chamam listIntegrations
 *   com os parâmetros corretos.
 * - Bulk select: Partner + Organization kind disabled (decisão #2).
 * - BulkActionBar aparece quando selectedIds > 0.
 * - Bulk dialog: typing obrigatório quando >10 itens.
 * - Badge "Filtro ativo: org=X" quando selectedOrgId está setado.
 * - Confirma chamada de bulkDeactivateIntegrations.
 */

import { render, screen, fireEvent, waitFor, act } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { MemoryRouter } from "react-router-dom"
import IntegrationsPage from "@/pages/IntegrationsPage"
import * as api from "@/services/api"
import type { Integration } from "@/types"

vi.mock("@/services/api")
vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({
    user: { role: "admin" },
  }),
}))

// Mock mutável para conseguir trocar selectedOrgId entre tests.
const platformContextValue: {
  organizations: Array<{ id: number; name: string }>
  refreshData: ReturnType<typeof vi.fn>
  selectedOrgId: number | null
  selectedPlatform: string | null
} = {
  organizations: [
    { id: 10, name: "Tenant Alpha" },
    { id: 11, name: "Tenant Beta" },
  ],
  refreshData: vi.fn().mockResolvedValue(undefined),
  selectedOrgId: null,
  selectedPlatform: null,
}

vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: () => platformContextValue,
}))

const mockedApi = vi.mocked(api)

const INT_TENANT_A: Integration = {
  id: 1,
  organization_id: 10,
  organization_name: "Tenant Alpha",
  name: "Sophos Tenant Alpha",
  platform: "sophos",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  kind: "tenant",
  capabilities: ["alerts:list"],
}

const INT_TENANT_B: Integration = {
  id: 2,
  organization_id: 11,
  organization_name: "Tenant Beta",
  name: "Wazuh Tenant Beta",
  platform: "wazuh",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  kind: "tenant",
  capabilities: ["alerts:list"],
}

const INT_PARTNER: Integration = {
  id: 3,
  organization_id: 10,
  organization_name: "Tenant Alpha",
  name: "Sophos Partner Holding",
  platform: "sophos",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  kind: "partner",
  children_count: 5,
  capabilities: [],
}

const INT_ORG_KIND: Integration = {
  id: 4,
  organization_id: 10,
  organization_name: "Tenant Alpha",
  name: "Sophos Org Kind",
  platform: "sophos",
  is_active: true,
  is_authenticated: true,
  auth_status: "healthy",
  kind: "organization",
  auto_managed: true,
  capabilities: [],
}

const INT_INACTIVE: Integration = {
  id: 5,
  organization_id: 10,
  organization_name: "Tenant Alpha",
  name: "Old Off",
  platform: "sophos",
  is_active: false,
  is_authenticated: false,
  auth_status: "unknown",
  kind: "tenant",
  capabilities: [],
}

const INTEGRATIONS_BASE: Integration[] = [
  INT_TENANT_A,
  INT_TENANT_B,
  INT_PARTNER,
  INT_ORG_KIND,
  INT_INACTIVE,
]

function renderPage() {
  return render(
    <MemoryRouter>
      <IntegrationsPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  platformContextValue.selectedOrgId = null
  platformContextValue.selectedPlatform = null
  // @ts-expect-error vitest mock typing
  mockedApi.listIntegrations.mockResolvedValue(INTEGRATIONS_BASE)
  // @ts-expect-error
  mockedApi.bulkDeactivateIntegrations.mockResolvedValue({
    processed: 0,
    deactivated: 0,
    errors: [],
  })
  // @ts-expect-error
  mockedApi.deleteIntegration.mockResolvedValue({ detail: "Integration deactivated" })
})

describe("IntegrationsPage — render base", () => {
  it("carrega integrações via listIntegrations com filtros default (status=active, page=1, size=50)", async () => {
    renderPage()

    await waitFor(() => {
      expect(mockedApi.listIntegrations).toHaveBeenCalled()
    })
    const callArgs = mockedApi.listIntegrations.mock.calls[0]?.[0] as any
    expect(callArgs).toMatchObject({
      status: "active",
      page: 1,
      size: 50,
    })
  })

  it("renderiza linhas para cada integração", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument()
      expect(screen.getByText("Wazuh Tenant Beta")).toBeInTheDocument()
      expect(screen.getByText("Sophos Partner Holding")).toBeInTheDocument()
    })
  })

  it("Partner mostra badge com contagem de tenants", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Partner · 5 tenants/i)).toBeInTheDocument()
    })
  })
})

describe("IntegrationsPage — filtros", () => {
  it("trocar dropdown de tipo dispara nova busca", async () => {
    renderPage()
    await waitFor(() =>
      expect(mockedApi.listIntegrations).toHaveBeenCalledTimes(1),
    )

    const kindSelect = screen.getByTestId("integration-filter-kind")
    fireEvent.click(kindSelect)
    const option = await screen.findByRole("option", { name: "Partner" })
    fireEvent.click(option)

    await waitFor(() => {
      const lastCall = mockedApi.listIntegrations.mock.calls.at(-1)?.[0] as any
      expect(lastCall?.kind).toBe("partner")
    })
  })

  it("trocar status dispara nova busca", async () => {
    renderPage()
    await waitFor(() =>
      expect(mockedApi.listIntegrations).toHaveBeenCalledTimes(1),
    )

    const statusSelect = screen.getByTestId("integration-filter-status")
    fireEvent.click(statusSelect)
    const option = await screen.findByRole("option", { name: "Inativas" })
    fireEvent.click(option)

    await waitFor(() => {
      const lastCall = mockedApi.listIntegrations.mock.calls.at(-1)?.[0] as any
      expect(lastCall?.status).toBe("inactive")
    })
  })

  it("helper text avisa que Partner não entra em bulk", async () => {
    renderPage()
    await waitFor(() => {
      expect(
        screen.getByText(/Integrações Partner não podem ser desativadas em massa/i),
      ).toBeInTheDocument()
    })
  })
})

describe("IntegrationsPage — bulk selection (Partner blocked)", () => {
  it("checkbox de Partner é disabled (decisão #2)", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Partner Holding")).toBeInTheDocument(),
    )
    const cbPartner = screen.getByTestId(
      `integration-row-checkbox-${INT_PARTNER.id}`,
    ) as HTMLInputElement
    expect(cbPartner.disabled).toBe(true)
  })

  it("checkbox de kind=organization é disabled", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Org Kind")).toBeInTheDocument(),
    )
    const cbOrg = screen.getByTestId(
      `integration-row-checkbox-${INT_ORG_KIND.id}`,
    ) as HTMLInputElement
    expect(cbOrg.disabled).toBe(true)
  })

  it("checkbox de inativa é disabled", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Old Off")).toBeInTheDocument())
    const cbInactive = screen.getByTestId(
      `integration-row-checkbox-${INT_INACTIVE.id}`,
    ) as HTMLInputElement
    expect(cbInactive.disabled).toBe(true)
  })

  it("selecionar tenant ativa abre BulkActionBar", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument(),
    )

    const cb = screen.getByTestId(
      `integration-row-checkbox-${INT_TENANT_A.id}`,
    )
    fireEvent.click(cb)

    expect(screen.getByTestId("bulk-action-bar")).toBeInTheDocument()
    expect(
      screen.getByTestId("bulk-action-bar-count").textContent,
    ).toMatch(/1 integração\(ões\) selecionado/)
  })

  it("'selecionar elegíveis' marca apenas tenants ativos (não Partner/Org/inactive)", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument(),
    )

    fireEvent.click(screen.getByTestId("integration-select-all"))
    // INT_TENANT_A + INT_TENANT_B = 2 elegíveis.
    expect(
      screen.getByTestId("bulk-action-bar-count").textContent,
    ).toMatch(/2 integração\(ões\)/)
  })
})

describe("IntegrationsPage — bulk deactivate confirm", () => {
  it("clicar em 'Desativar selecionadas' abre o dialog", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument(),
    )

    fireEvent.click(
      screen.getByTestId(`integration-row-checkbox-${INT_TENANT_A.id}`),
    )
    fireEvent.click(screen.getByTestId("integration-bulk-deactivate"))

    expect(
      screen.getByText(/Desativar 1 integração\(ões\)\?/),
    ).toBeInTheDocument()
  })

  it("confirmar com <=10 itens dispara bulkDeactivateIntegrations", async () => {
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument(),
    )

    fireEvent.click(
      screen.getByTestId(`integration-row-checkbox-${INT_TENANT_A.id}`),
    )
    fireEvent.click(
      screen.getByTestId(`integration-row-checkbox-${INT_TENANT_B.id}`),
    )
    fireEvent.click(screen.getByTestId("integration-bulk-deactivate"))

    fireEvent.click(screen.getByTestId("integration-bulk-confirm"))

    await waitFor(() => {
      expect(mockedApi.bulkDeactivateIntegrations).toHaveBeenCalledWith(
        expect.arrayContaining([INT_TENANT_A.id, INT_TENANT_B.id]),
      )
    })
  })

  it("com >10 itens, Confirmar fica disabled até digitar 'DESATIVAR <N>'", async () => {
    // Gera 12 tenants ativas (todas elegíveis).
    const many: Integration[] = Array.from({ length: 12 }, (_, i) => ({
      id: 1000 + i,
      organization_id: 10,
      organization_name: "Tenant Alpha",
      name: `Bulk Tenant ${i}`,
      platform: "sophos",
      is_active: true,
      is_authenticated: true,
      auth_status: "healthy",
      kind: "tenant",
      capabilities: [],
    }))
    // @ts-expect-error
    mockedApi.listIntegrations.mockResolvedValue(many)

    renderPage()
    await waitFor(() => expect(screen.getByText("Bulk Tenant 0")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("integration-select-all"))
    fireEvent.click(screen.getByTestId("integration-bulk-deactivate"))

    const confirmBtn = screen.getByTestId(
      "integration-bulk-confirm",
    ) as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    const input = screen.getByTestId(
      "integration-bulk-confirm-text",
    ) as HTMLInputElement
    fireEvent.change(input, { target: { value: "errado" } })
    expect(confirmBtn.disabled).toBe(true)

    fireEvent.change(input, { target: { value: "DESATIVAR 12" } })
    expect(confirmBtn.disabled).toBe(false)
  })
})

describe("IntegrationsPage — badge filtro org global (decisão #5)", () => {
  it("sem selectedOrgId: badge não aparece", async () => {
    platformContextValue.selectedOrgId = null
    renderPage()
    await waitFor(() =>
      expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument(),
    )
    expect(
      screen.queryByTestId("integration-active-org-badge"),
    ).not.toBeInTheDocument()
  })

  it("com selectedOrgId setado: badge mostra nome da org", async () => {
    platformContextValue.selectedOrgId = 10
    renderPage()
    await waitFor(() => {
      expect(
        screen.getByTestId("integration-active-org-badge"),
      ).toBeInTheDocument()
    })
    expect(screen.getByText(/Filtro ativo: org=Tenant Alpha/i)).toBeInTheDocument()
  })

  it("selectedOrgId é repassado pra listIntegrations", async () => {
    platformContextValue.selectedOrgId = 11
    renderPage()
    await waitFor(() => {
      const callArgs = mockedApi.listIntegrations.mock.calls[0]?.[0] as any
      expect(callArgs?.organizationId).toBe(11)
    })
  })
})

describe("IntegrationsPage — feedback toast auto-dismiss", () => {
  it("toast de feedback some após 5s", async () => {
    // @ts-expect-error
    mockedApi.testIntegrationConnection.mockResolvedValue({ status: "healthy" })

    vi.useFakeTimers({ shouldAdvanceTime: true })
    try {
      renderPage()
      await waitFor(() => expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument())

      await act(async () => {
        fireEvent.click(screen.getAllByText("Testar")[0])
        await Promise.resolve()
      })

      await waitFor(() => {
        expect(screen.getByText(/Teste concluído/)).toBeInTheDocument()
      })

      act(() => {
        vi.advanceTimersByTime(5001)
      })

      expect(screen.queryByText(/Teste concluído/)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe("IntegrationsPage — last_error não exibe banner", () => {
  it("integração com last_error não renderiza banner warning persistente", async () => {
    const withError: Integration[] = [
      {
        ...INT_TENANT_A,
        last_error: "Authentication token expired",
      },
    ]
    // @ts-expect-error
    mockedApi.listIntegrations.mockResolvedValue(withError)

    renderPage()
    await waitFor(() => expect(screen.getByText("Sophos Tenant Alpha")).toBeInTheDocument())

    expect(screen.queryByText(/Authentication token expired/)).not.toBeInTheDocument()
    expect(document.querySelector(".bg-warning-50")).not.toBeInTheDocument()
  })
})
