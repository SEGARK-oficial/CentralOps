/**
 * ConfigPage — ErrorState com retry por aba.
 *
 * As 4 abas orientadas a hook (email/collector/identity/mcp) SÓ mostravam o
 * erro (`Notice`) sem nenhuma ação: o operador tinha que dar F5 na página
 * inteira pra tentar de novo. Os hooks já expunham `refetch` — este teste
 * trava que a página de fato o usa via `ErrorState` (título + retry), uma
 * aba por vez, e prova que o retry chama o `refetch` DAQUELA aba e não de
 * outra (senão um clique na aba errada re-buscaria o config errado).
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { vi } from "vitest"
import { ConfigPage } from "@/pages/ConfigPage"
import { useEmailConfig } from "@/hooks/useEmailConfig"
import { useCollectorConfig } from "@/hooks/useCollectorConfig"
import { useIdentityConfig } from "@/hooks/useIdentityConfig"
import { useMcpConfig } from "@/hooks/useMcpConfig"
import * as api from "@/services/api"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/useEmailConfig")
vi.mock("@/hooks/useCollectorConfig")
vi.mock("@/hooks/useIdentityConfig")
vi.mock("@/hooks/useMcpConfig")

// Componentes fora do escopo deste teste (edição/licença, captura,
// enriquecimento, os formulários em si) — stubs triviais para isolar o
// comportamento de erro/retry das 4 abas orientadas a hook.
vi.mock("@/components/config/EditionInfoCard", () => ({
  EditionInfoCard: () => null,
}))
vi.mock("@/components/config/CapturePanel", () => ({
  CapturePanel: () => null,
}))
vi.mock("@/components/config/EnrichmentConfigForm", () => ({
  EnrichmentConfigForm: () => null,
}))
vi.mock("@/components/config/LicenseActivationForm", () => ({
  LicenseActivationForm: () => null,
}))
vi.mock("@/components/config/EmailConfigForm", () => ({
  EmailConfigForm: () => <div data-testid="email-form" />,
}))
vi.mock("@/components/config/CollectorConfigForm", () => ({
  CollectorConfigForm: () => <div data-testid="collector-form" />,
}))
vi.mock("@/components/config/IdentityConfigForm", () => ({
  IdentityConfigForm: () => <div data-testid="identity-form" />,
}))
vi.mock("@/components/config/McpConfigForm", () => ({
  McpConfigForm: () => <div data-testid="mcp-form" />,
}))

const mockedUseEmailConfig = vi.mocked(useEmailConfig)
const mockedUseCollectorConfig = vi.mocked(useCollectorConfig)
const mockedUseIdentityConfig = vi.mocked(useIdentityConfig)
const mockedUseMcpConfig = vi.mocked(useMcpConfig)

const refetchEmail = vi.fn()
const refetchCollector = vi.fn()
const refetchIdentity = vi.fn()
const refetchMcp = vi.fn()

function baseEmail(overrides: Partial<ReturnType<typeof useEmailConfig>> = {}) {
  return {
    config: null,
    recipients: [],
    loading: false,
    saving: false,
    testing: false,
    addingRecipient: false,
    removingRecipientId: null,
    error: null,
    feedback: null,
    saveConfig: vi.fn(),
    addRecipient: vi.fn(),
    removeRecipient: vi.fn(),
    sendTest: vi.fn(),
    clearFeedback: vi.fn(),
    refetch: refetchEmail,
    ...overrides,
  }
}

function baseCollector(overrides: Partial<ReturnType<typeof useCollectorConfig>> = {}) {
  return {
    config: null,
    loading: false,
    saving: false,
    error: null,
    feedback: null,
    saveConfig: vi.fn(),
    clearFeedback: vi.fn(),
    refetch: refetchCollector,
    ...overrides,
  }
}

function baseIdentity(overrides: Partial<ReturnType<typeof useIdentityConfig>> = {}) {
  return {
    config: null,
    loading: false,
    saving: false,
    testing: false,
    testResult: null,
    error: null,
    feedback: null,
    saveConfig: vi.fn(),
    testConnection: vi.fn(),
    clearFeedback: vi.fn(),
    refetch: refetchIdentity,
    ...overrides,
  }
}

function baseMcp(overrides: Partial<ReturnType<typeof useMcpConfig>> = {}) {
  return {
    config: null,
    loading: false,
    saving: false,
    error: null,
    feedback: null,
    saveConfig: vi.fn(),
    clearFeedback: vi.fn(),
    refetch: refetchMcp,
    ...overrides,
  }
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ConfigPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUseEmailConfig.mockReturnValue(baseEmail())
  mockedUseCollectorConfig.mockReturnValue(baseCollector())
  mockedUseIdentityConfig.mockReturnValue(baseIdentity())
  mockedUseMcpConfig.mockReturnValue(baseMcp())
  vi.spyOn(api, "listDestinations").mockResolvedValue([])
})

describe("ConfigPage — ErrorState com retry", () => {
  it("aba email: sem erro, não renderiza ErrorState", () => {
    renderPage()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.getByTestId("email-form")).toBeInTheDocument()
  })

  it("aba email: com erro, mostra ErrorState e o retry chama refetch do email (só dele)", () => {
    mockedUseEmailConfig.mockReturnValue(baseEmail({ error: "falha de rede" }))
    renderPage()

    expect(screen.getByRole("alert")).toHaveTextContent("Falha ao carregar configuração de email")
    expect(screen.getByRole("alert")).toHaveTextContent("falha de rede")

    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    expect(refetchEmail).toHaveBeenCalledTimes(1)
    expect(refetchCollector).not.toHaveBeenCalled()
    expect(refetchIdentity).not.toHaveBeenCalled()
    expect(refetchMcp).not.toHaveBeenCalled()
  })

  it("aba collector: com erro, o retry chama refetch do collector (só dele)", () => {
    mockedUseCollectorConfig.mockReturnValue(baseCollector({ error: "falha do collector" }))
    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /coleta.*entrega/i }))
    expect(screen.getByRole("alert")).toHaveTextContent("Falha ao carregar configuração do Collector")

    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    expect(refetchCollector).toHaveBeenCalledTimes(1)
    expect(refetchEmail).not.toHaveBeenCalled()
    expect(refetchIdentity).not.toHaveBeenCalled()
    expect(refetchMcp).not.toHaveBeenCalled()
  })

  it("aba identity: com erro, o retry chama refetch da identity (só dela)", () => {
    mockedUseIdentityConfig.mockReturnValue(baseIdentity({ error: "falha de identidade" }))
    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /identidade.*sso/i }))
    expect(screen.getByRole("alert")).toHaveTextContent("Falha ao carregar configuração de identidade")

    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    expect(refetchIdentity).toHaveBeenCalledTimes(1)
    expect(refetchEmail).not.toHaveBeenCalled()
    expect(refetchCollector).not.toHaveBeenCalled()
    expect(refetchMcp).not.toHaveBeenCalled()
  })

  it("aba mcp: com erro, o retry chama refetch do mcp (só dele)", () => {
    mockedUseMcpConfig.mockReturnValue(baseMcp({ error: "falha do mcp" }))
    renderPage()

    fireEvent.click(screen.getByRole("tab", { name: /assistentes.*mcp/i }))
    expect(screen.getByRole("alert")).toHaveTextContent("Falha ao carregar a configuração do servidor MCP")

    fireEvent.click(screen.getByRole("button", { name: /tentar novamente/i }))

    expect(refetchMcp).toHaveBeenCalledTimes(1)
    expect(refetchEmail).not.toHaveBeenCalled()
    expect(refetchCollector).not.toHaveBeenCalled()
    expect(refetchIdentity).not.toHaveBeenCalled()
  })
})
