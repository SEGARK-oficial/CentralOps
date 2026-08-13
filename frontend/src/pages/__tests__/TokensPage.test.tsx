import { fireEvent, render, screen, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { TokensPage } from "@/pages/TokensPage"
import type { ApiToken } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api", () => ({
  listApiTokens: vi.fn(),
  createApiToken: vi.fn(),
  revokeApiToken: vi.fn(),
  // Fase 2 — TokensPage agora carrega lista de scopes via ScopeSelector.
  listScopes: vi.fn().mockResolvedValue([
    "mapping.read",
    "integration.read",
    "audit.read",
  ]),
}))

import * as api from "@/services/api"

const mockToken = (overrides: Partial<ApiToken> = {}): ApiToken => ({
  id: 1,
  name: "ci-bot",
  token_prefix: "copsk_aB3xK7",
  user_id: 42,
  service_account_id: null,
  expires_at: "2027-01-01T00:00:00Z",
  is_eternal: false,
  scopes: null,
  last_used_at: null,
  last_used_ip: null,
  use_count: 0,
  revoked_at: null,
  created_at: "2026-05-01T00:00:00Z",
  ...overrides,
})

describe("TokensPage", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("renderiza empty state quando nao ha tokens", async () => {
    ;(api.listApiTokens as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    render(<TokensPage />)
    await waitFor(() => {
      expect(screen.getByText(/Nenhum token criado/i)).toBeInTheDocument()
    })
  })

  it("renderiza lista de tokens com prefixo e status", async () => {
    ;(api.listApiTokens as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      mockToken(),
      mockToken({
        id: 2,
        name: "expired-bot",
        revoked_at: "2026-04-01T00:00:00Z",
      }),
    ])
    render(<TokensPage />)
    // Escopo na tabela desktop (cards md:hidden duplicam o texto no jsdom).
    const table = await screen.findByRole("table")
    expect(within(table).getByText("ci-bot")).toBeInTheDocument()
    expect(within(table).getByText("expired-bot")).toBeInTheDocument()
    expect(within(table).getByText("Revogado")).toBeInTheDocument()
    expect(within(table).getByText("Ativo")).toBeInTheDocument()
    // Prefix e mostrado
    expect(within(table).getAllByText(/copsk_aB3xK7/).length).toBeGreaterThanOrEqual(1)
  })

  it("token sem expires_at mostra badge Eterno", async () => {
    ;(api.listApiTokens as ReturnType<typeof vi.fn>).mockResolvedValueOnce([
      mockToken({ expires_at: null }),
    ])
    render(<TokensPage />)
    const table = await screen.findByRole("table")
    expect(within(table).getByText("Eterno")).toBeInTheDocument()
    expect(within(table).getByText(/Nunca expira/)).toBeInTheDocument()
  })

  it("abre modal de criacao ao clicar em 'Novo token'", async () => {
    ;(api.listApiTokens as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    render(<TokensPage />)
    // espera o EmptyState renderizar (mostra o segundo botao "Novo token")
    await waitFor(() => screen.getByText(/Nenhum token criado/i))
    // clica no botao do header (primeiro)
    const buttons = screen.getAllByRole("button", { name: /Novo token/i })
    fireEvent.click(buttons[0])
    expect(
      screen.getByRole("heading", { name: /Novo Personal Access Token/i }),
    ).toBeInTheDocument()
  })

  it("modal mostra warning amarelo ao escolher 'Nunca expira'", async () => {
    ;(api.listApiTokens as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    render(<TokensPage />)
    await waitFor(() => screen.getByText(/Nenhum token criado/i))
    const buttons = screen.getAllByRole("button", { name: /Novo token/i })
    fireEvent.click(buttons[0])

    const select = screen.getByLabelText(/Expira em/i) as HTMLSelectElement
    fireEvent.change(select, { target: { value: "never" } })

    expect(screen.getByText(/sem expiração/i)).toBeInTheDocument()
  })

  it("bloqueia criar token 'least privilege' com zero scopes marcados", async () => {
    // O backend trata `[]` e `null` igual (`effective_scopes` faz
    // `if not token_scopes`, e `not []` é verdadeiro em Python), então um token
    // submetido nesse estado herdaria a role INTEIRA enquanto a tela diz
    // "least privilege". O usuário receberia o OPOSTO do que pediu.
    ;(api.listApiTokens as ReturnType<typeof vi.fn>).mockResolvedValueOnce([])
    render(<TokensPage />)
    await waitFor(() => screen.getByText(/Nenhum token criado/i))
    fireEvent.click(screen.getAllByRole("button", { name: /Novo token/i })[0])

    fireEvent.change(screen.getByLabelText(/Nome/i), { target: { value: "bot" } })

    // Escolhe restringir e NÃO marca nenhum scope.
    await screen.findByText(/Restringir a scopes específicos/i)
    fireEvent.click(screen.getByText(/Restringir a scopes específicos/i))

    fireEvent.click(screen.getByRole("button", { name: /Criar token/i }))

    await waitFor(() =>
      expect(screen.getByText(/não marcou nenhum/i)).toBeInTheDocument(),
    )
    expect(api.createApiToken).not.toHaveBeenCalled()
  })

  it("expoe raw token uma unica vez apos criar", async () => {
    ;(api.listApiTokens as ReturnType<typeof vi.fn>)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([mockToken()])
    ;(api.createApiToken as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      token: "copsk_aB3xK7zY9MmRTpFqZqVm5e8XvU4jWkH7c0n1L2gIo67Y",
      api_token: mockToken(),
    })

    render(<TokensPage />)
    await waitFor(() => screen.getByText(/Nenhum token criado/i))

    const buttons = screen.getAllByRole("button", { name: /Novo token/i })
    fireEvent.click(buttons[0])

    fireEvent.change(screen.getByLabelText(/Nome/i), {
      target: { value: "my-bot" },
    })
    fireEvent.click(screen.getByRole("button", { name: /Criar token/i }))

    await waitFor(() => {
      expect(screen.getByTestId("created-raw-token")).toBeInTheDocument()
    })
    expect(screen.getByTestId("created-raw-token").textContent).toContain(
      "copsk_aB3xK7zY9MmRTpFqZqVm",
    )
    // Warning de "copie agora"
    expect(screen.getByText(/Copie o token agora/i)).toBeInTheDocument()
  })
})
