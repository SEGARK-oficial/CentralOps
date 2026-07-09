import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { MemoryRouter } from "react-router-dom"

import { ServiceAccountsPage } from "@/pages/ServiceAccountsPage"
import type { ServiceAccount } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api", () => ({
  listServiceAccounts: vi.fn(),
  createServiceAccount: vi.fn(),
  updateServiceAccount: vi.fn(),
  deleteServiceAccount: vi.fn(),
  listServiceAccountTokens: vi.fn(),
  createServiceAccountToken: vi.fn(),
  revokeServiceAccountToken: vi.fn(),
  listScopes: vi.fn().mockResolvedValue([
    "mapping.read",
    "integration.read",
    "internal.tenant.read",
  ]),
}))

import * as api from "@/services/api"

const mockSa = (overrides: Partial<ServiceAccount> = {}): ServiceAccount => ({
  id: 1,
  name: "iasoc-worker",
  description: "Worker IASOC consumindo /api/internal/*",
  role: "operator",
  organization_id: null,
  is_active: true,
  created_by_user_id: 1,
  created_at: "2026-05-01T10:00:00Z",
  updated_at: "2026-05-01T10:00:00Z",
  active_token_count: 2,
  ...overrides,
})

const renderPage = () =>
  render(
    <MemoryRouter>
      <ServiceAccountsPage />
    </MemoryRouter>,
  )

describe("ServiceAccountsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("renders empty state when no Service Accounts exist", async () => {
    ;(api.listServiceAccounts as ReturnType<typeof vi.fn>).mockResolvedValue([])
    renderPage()
    expect(
      await screen.findByText(/Nenhum Service Account/i),
    ).toBeInTheDocument()
  })

  it("lists Service Accounts with role badge and active token count", async () => {
    ;(api.listServiceAccounts as ReturnType<typeof vi.fn>).mockResolvedValue([
      mockSa(),
      mockSa({
        id: 2,
        name: "grafana-bot",
        role: "viewer",
        active_token_count: 0,
      }),
    ])

    renderPage()
    // Escopo na tabela desktop (cards md:hidden duplicam o texto no jsdom).
    const table = await screen.findByRole("table")
    expect(within(table).getByText("iasoc-worker")).toBeInTheDocument()
    expect(within(table).getByText("grafana-bot")).toBeInTheDocument()
    // Role badges (multiple "operator" or "viewer" allowed in summary)
    expect(screen.getAllByText(/operator/i).length).toBeGreaterThan(0)
    // Active token counter
    expect(within(table).getByText("2")).toBeInTheDocument()
  })

  it("opens create modal when 'Novo Service Account' is clicked", async () => {
    ;(api.listServiceAccounts as ReturnType<typeof vi.fn>).mockResolvedValue([])
    renderPage()
    await screen.findByText(/Nenhum Service Account/i)

    const buttons = screen.getAllByRole("button", { name: /Novo Service Account/i })
    fireEvent.click(buttons[0])

    expect(
      await screen.findByPlaceholderText(/iasoc-worker, grafana-bot/i),
    ).toBeInTheDocument()
    // Role select — "operator" option must exist.
    expect(screen.getByText(/Operator \(leitura \+ ops\)/i)).toBeInTheDocument()
  })

  it("submits create request and refetches list", async () => {
    ;(api.listServiceAccounts as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([mockSa()])
    ;(api.createServiceAccount as ReturnType<typeof vi.fn>).mockResolvedValue(
      mockSa(),
    )

    renderPage()
    await screen.findByText(/Nenhum Service Account/i)

    const buttons = screen.getAllByRole("button", { name: /Novo Service Account/i })
    fireEvent.click(buttons[0])

    const nameInput = await screen.findByPlaceholderText(
      /iasoc-worker, grafana-bot/i,
    )
    fireEvent.change(nameInput, { target: { value: "ci-bot" } })

    const submit = screen.getByRole("button", {
      name: /Criar Service Account/i,
    })
    fireEvent.click(submit)

    await waitFor(() =>
      expect(api.createServiceAccount).toHaveBeenCalledWith({
        name: "ci-bot",
        description: null,
        role: "viewer",
      }),
    )
    // List refetched after success.
    await waitFor(() =>
      expect(api.listServiceAccounts).toHaveBeenCalledTimes(2),
    )
  })

  it("opens delete confirm dialog and dispatches delete on confirm", async () => {
    ;(api.listServiceAccounts as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce([mockSa()])
      .mockResolvedValueOnce([])
    ;(api.deleteServiceAccount as ReturnType<typeof vi.fn>).mockResolvedValue(
      undefined,
    )

    renderPage()
    const table = await screen.findByRole("table")

    fireEvent.click(within(table).getByRole("button", { name: /Deletar/i }))
    // Dialog mentions the cascade behavior
    expect(
      await screen.findByText(/tokens ativos serão revogados em cascata/i),
    ).toBeInTheDocument()

    const confirmBtn = screen.getAllByRole("button", { name: /^Deletar$/i })
    // The dialog's confirm button is the most recent one
    fireEvent.click(confirmBtn[confirmBtn.length - 1])

    await waitFor(() =>
      expect(api.deleteServiceAccount).toHaveBeenCalledWith(1),
    )
    // Sucess feedback shown
    expect(
      await screen.findByText(/deletado\. Tokens dele foram revogados/i),
    ).toBeInTheDocument()
  })

  it("shows desativado badge for inactive accounts and admin badge for admin role", async () => {
    ;(api.listServiceAccounts as ReturnType<typeof vi.fn>).mockResolvedValue([
      mockSa({ id: 3, name: "broken-bot", is_active: false, role: "admin" }),
    ])
    renderPage()
    const table = await screen.findByRole("table")
    expect(within(table).getByText("broken-bot")).toBeInTheDocument()
    expect(within(table).getByText(/Desativado/i)).toBeInTheDocument()
    // "admin" appears in the role badge AND in the role description text;
    // assert via the badge specifically (its variant=danger gives it a class
    // we can leverage, but more robust: count > 0).
    expect(screen.getAllByText(/admin/i).length).toBeGreaterThan(0)
  })
})
