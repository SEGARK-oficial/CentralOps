/**
 * OrganizationsPage tests (bulk + filtros).
 *
 * Cobre:
 * - Filtros (status / auto_managed / busca debounced) chamam listOrganizations
 *   com os parâmetros corretos.
 * - Bulk select: header checkbox indeterminate/checked, auto_managed disabled.
 * - BulkActionBar aparece quando selectedIds > 0.
 * - Bulk dialog: ConfirmDialog substituindo confirm() nativo, typing
 *   obrigatório quando >10 itens.
 * - Single delete usa ConfirmDialog (não confirm() nativo).
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { MemoryRouter } from "react-router-dom"
import OrganizationsPage from "@/pages/OrganizationsPage"
import * as api from "@/services/api"
import type { Organization } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: () => ({
    refreshData: vi.fn().mockResolvedValue(undefined),
  }),
}))

// Estado de edição mutável por teste (default: sem teto → comportamento Community).
const editionState = vi.hoisted(() => ({ maxOrganizations: null as number | null }))
vi.mock("@/contexts/EditionContext", () => ({
  useEdition: () => ({
    edition: "community",
    features: [],
    plan: null,
    seats: null,
    maxOrganizations: editionState.maxOrganizations,
    expiresAt: null,
    isEnterprise: false,
    loading: false,
    error: null,
    hasFeature: () => false,
    refresh: vi.fn(),
  }),
}))

const mockedApi = vi.mocked(api)

const ORG_ACTIVE_A: Organization = {
  id: 1,
  name: "Acme Corp",
  slug: "acme-corp",
  description: null,
  is_active: true,
  integration_count: 2,
  auto_managed: false,
}

const ORG_ACTIVE_B: Organization = {
  id: 2,
  name: "Globex",
  slug: "globex",
  description: null,
  is_active: true,
  integration_count: 0,
  auto_managed: false,
}

const ORG_AUTO: Organization = {
  id: 3,
  name: "Sophos Auto Org",
  slug: "sophos-auto",
  description: null,
  is_active: true,
  integration_count: 1,
  auto_managed: true,
  external_provider: "sophos",
}

const ORGS_BASE: Organization[] = [ORG_ACTIVE_A, ORG_ACTIVE_B, ORG_AUTO]

function renderPage() {
  return render(
    <MemoryRouter>
      <OrganizationsPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  editionState.maxOrganizations = null
  // @ts-expect-error vitest mock typing
  mockedApi.countActiveOrganizations.mockResolvedValue(0)
  // @ts-expect-error vitest mock typing
  mockedApi.listOrganizations.mockResolvedValue(ORGS_BASE)
  // @ts-expect-error
  mockedApi.bulkDeactivateOrganizations.mockResolvedValue({
    processed: 0,
    deactivated: 0,
    errors: [],
  })
  // @ts-expect-error
  mockedApi.deleteOrganization.mockResolvedValue(undefined)
})

describe("OrganizationsPage — render base", () => {
  it("carrega organizações via listOrganizations com filtros default", async () => {
    renderPage()

    await waitFor(() => {
      expect(mockedApi.listOrganizations).toHaveBeenCalled()
    })
    const callArgs = mockedApi.listOrganizations.mock.calls[0]?.[0]
    expect(callArgs).toMatchObject({
      status: "active",
      autoManaged: "all",
      page: 1,
      size: 50,
    })
  })

  it("renderiza linhas para cada organização", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText("Acme Corp")).toBeInTheDocument()
      expect(screen.getByText("Globex")).toBeInTheDocument()
      expect(screen.getByText("Sophos Auto Org")).toBeInTheDocument()
    })
  })

  it("auto-managed mostra badge 'Auto-gerenciado'", async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText(/Auto-gerenciado/)).toBeInTheDocument()
    })
  })
})

describe("OrganizationsPage — filtros", () => {
  it("mudar dropdown de status dispara nova busca", async () => {
    renderPage()
    await waitFor(() => expect(mockedApi.listOrganizations).toHaveBeenCalledTimes(1))

    const statusSelect = screen.getByTestId("org-filter-status")
    // Select é portal-based — abrimos com clique e selecionamos opção.
    fireEvent.click(statusSelect)
    const option = await screen.findByRole("option", { name: "Inativas" })
    fireEvent.click(option)

    await waitFor(() => {
      const lastCall = mockedApi.listOrganizations.mock.calls.at(-1)?.[0] as any
      expect(lastCall?.status).toBe("inactive")
    })
  })

  it("mudar dropdown auto_managed dispara nova busca", async () => {
    renderPage()
    await waitFor(() => expect(mockedApi.listOrganizations).toHaveBeenCalledTimes(1))

    const select = screen.getByTestId("org-filter-auto-managed")
    fireEvent.click(select)
    const option = await screen.findByRole("option", { name: "Apenas manuais" })
    fireEvent.click(option)

    await waitFor(() => {
      const lastCall = mockedApi.listOrganizations.mock.calls.at(-1)?.[0] as any
      expect(lastCall?.autoManaged).toBe("false")
    })
  })

  it("mostra helper text sobre auto-managed", async () => {
    renderPage()
    await waitFor(() => {
      expect(
        screen.getByText(/Organizações auto-gerenciadas \(Sophos\)/i),
      ).toBeInTheDocument()
    })
  })
})

describe("OrganizationsPage — bulk selection", () => {
  it("checkbox da linha auto-managed está disabled", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Sophos Auto Org")).toBeInTheDocument())
    const cbAuto = screen.getByTestId(`org-row-checkbox-${ORG_AUTO.id}`) as HTMLInputElement
    expect(cbAuto.disabled).toBe(true)
  })

  it("selecionar uma org abre BulkActionBar", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    const cb = screen.getByTestId(`org-row-checkbox-${ORG_ACTIVE_A.id}`)
    fireEvent.click(cb)

    expect(screen.getByTestId("bulk-action-bar")).toBeInTheDocument()
    expect(screen.getByTestId("bulk-action-bar-count").textContent).toMatch(
      /1 organização\(ões\) selecionado/,
    )
  })

  it("'selecionar elegíveis' marca apenas non-auto-managed", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("org-select-all"))
    // Acme + Globex = 2; auto org não conta.
    expect(screen.getByTestId("bulk-action-bar-count").textContent).toMatch(
      /2 organização\(ões\)/,
    )
  })
})

describe("OrganizationsPage — bulk deactivate confirm", () => {
  it("clicar em 'Desativar selecionadas' abre o dialog", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId(`org-row-checkbox-${ORG_ACTIVE_A.id}`))
    fireEvent.click(screen.getByTestId("org-bulk-deactivate"))

    expect(
      screen.getByText(/Desativar 1 organização\(ões\)\?/),
    ).toBeInTheDocument()
  })

  it("confirmar com <=10 itens dispara bulkDeactivateOrganizations", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId(`org-row-checkbox-${ORG_ACTIVE_A.id}`))
    fireEvent.click(screen.getByTestId(`org-row-checkbox-${ORG_ACTIVE_B.id}`))
    fireEvent.click(screen.getByTestId("org-bulk-deactivate"))

    fireEvent.click(screen.getByTestId("org-bulk-confirm"))

    await waitFor(() => {
      expect(mockedApi.bulkDeactivateOrganizations).toHaveBeenCalledWith(
        expect.arrayContaining([ORG_ACTIVE_A.id, ORG_ACTIVE_B.id]),
      )
    })
  })

  it("com >10 itens, botão Confirmar fica disabled até digitar texto exato", async () => {
    // Gera 12 orgs ativas + auto.
    const many: Organization[] = Array.from({ length: 12 }, (_, i) => ({
      id: 100 + i,
      name: `Org ${i}`,
      slug: `org-${i}`,
      description: null,
      is_active: true,
      integration_count: 0,
      auto_managed: false,
    }))
    // @ts-expect-error
    mockedApi.listOrganizations.mockResolvedValue(many)

    renderPage()
    await waitFor(() => expect(screen.getByText("Org 0")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId("org-select-all"))
    fireEvent.click(screen.getByTestId("org-bulk-deactivate"))

    const confirmBtn = screen.getByTestId("org-bulk-confirm") as HTMLButtonElement
    expect(confirmBtn.disabled).toBe(true)

    const input = screen.getByTestId("org-bulk-confirm-text") as HTMLInputElement
    fireEvent.change(input, { target: { value: "errado" } })
    expect(confirmBtn.disabled).toBe(true)

    fireEvent.change(input, { target: { value: "DESATIVAR 12" } })
    expect(confirmBtn.disabled).toBe(false)
  })
})

describe("OrganizationsPage — single delete via ConfirmDialog (não confirm() nativo)", () => {
  it("clicar no trash abre ConfirmDialog em vez de window.confirm", async () => {
    const confirmSpy = vi.spyOn(window, "confirm")
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId(`org-delete-${ORG_ACTIVE_A.id}`))

    expect(screen.getByText(/Excluir organização\?/)).toBeInTheDocument()
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it("confirmar exclusão chama deleteOrganization com id certo", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    fireEvent.click(screen.getByTestId(`org-delete-${ORG_ACTIVE_A.id}`))

    fireEvent.click(screen.getByRole("button", { name: "Excluir" }))

    await waitFor(() => {
      expect(mockedApi.deleteOrganization).toHaveBeenCalledWith(ORG_ACTIVE_A.id)
    })
  })

  it("trash da auto-managed está disabled", async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText("Sophos Auto Org")).toBeInTheDocument())
    const btn = screen.getByTestId(`org-delete-${ORG_AUTO.id}`) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
  })
})

describe("OrganizationsPage — teto de orgs do tier", () => {
  it("sem teto (Community): sem badge de uso, botão habilitado, sem fetch de contagem", async () => {
    editionState.maxOrganizations = null
    renderPage()
    await waitFor(() => expect(screen.getByText("Acme Corp")).toBeInTheDocument())

    const btn = screen.getByRole("button", { name: /Nova Organização/i }) as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    expect(mockedApi.countActiveOrganizations).not.toHaveBeenCalled()
    expect(
      screen.queryByText(/Limite de organizações do plano atingido/),
    ).not.toBeInTheDocument()
  })

  it("no limite (Starter max=1, 1 ativa): badge 1/1, aviso e botão desabilitado", async () => {
    editionState.maxOrganizations = 1
    // @ts-expect-error vitest mock typing
    mockedApi.countActiveOrganizations.mockResolvedValue(1)
    renderPage()
    await waitFor(() => expect(mockedApi.countActiveOrganizations).toHaveBeenCalled())

    await waitFor(() => expect(screen.getByText(/1 \/ 1 organização/)).toBeInTheDocument())
    const btn = screen.getByRole("button", { name: /Nova Organização/i }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    expect(
      screen.getByText(/Limite de organizações do plano atingido/),
    ).toBeInTheDocument()
  })

  it("abaixo do teto (max=3, 1 ativa): badge 1/3 e botão habilitado", async () => {
    editionState.maxOrganizations = 3
    // @ts-expect-error vitest mock typing
    mockedApi.countActiveOrganizations.mockResolvedValue(1)
    renderPage()
    await waitFor(() => expect(mockedApi.countActiveOrganizations).toHaveBeenCalled())

    await waitFor(() => expect(screen.getByText(/1 \/ 3 organizações/)).toBeInTheDocument())
    const btn = screen.getByRole("button", { name: /Nova Organização/i }) as HTMLButtonElement
    expect(btn.disabled).toBe(false)
    expect(
      screen.queryByText(/Limite de organizações do plano atingido/),
    ).not.toBeInTheDocument()
  })
})
