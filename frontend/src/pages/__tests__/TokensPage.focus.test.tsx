import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest"
import { TokensPage } from "@/pages/TokensPage"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api", () => ({
  listApiTokens: vi.fn().mockResolvedValue([]),
  createApiToken: vi.fn(),
  revokeApiToken: vi.fn(),
  listScopes: vi.fn().mockResolvedValue(["mapping.read", "integration.read", "audit.read"]),
}))

describe("TokensPage — foco do modal de criação", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("mantém o foco no campo Nome ao digitar caractere a caractere", async () => {
    render(<TokensPage />)
    await waitFor(() => screen.getByText(/Nenhum token criado/i))

    fireEvent.click(screen.getAllByRole("button", { name: /Novo token/i })[0])

    const input = screen.getByLabelText(/Nome/i) as HTMLInputElement
    input.focus()
    expect(document.activeElement).toBe(input)

    const word = "ci-deploy-bot"
    for (let i = 0; i < word.length; i++) {
      const next = word.slice(0, i + 1)
      fireEvent.change(input, { target: { value: next } })
      // Após cada caractere o mesmo nó <input> deve continuar focado.
      expect(document.activeElement).toBe(input)
      expect((document.activeElement as HTMLInputElement).value).toBe(next)
    }
  })
})
