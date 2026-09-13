/**
 * Card "Assistentes de IA (MCP)" da página da conta: diz ao analista se o
 * servidor está ligado, se o papel dele tem `mcp.use`, e entrega o snippet de
 * cliente com a URL desta instância — sem nunca reexibir uma chave.
 */
import { render, screen, waitFor } from "@testing-library/react"
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest"

import AccountSettingsPage from "@/pages/AccountSettingsPage"
import * as api from "@/services/api"
import type { AccountProfile, McpStatus } from "@/types"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: () => ({ updateUser: vi.fn() }),
}))

const USER: AccountProfile = {
  id: "u-1",
  username: "alice",
  email: "alice@corp.example",
  display_name: "Alice",
  auth_provider: "local",
  is_global: false,
  organization_id: 3,
  organization_name: "Acme",
  role: "operator",
  is_active: true,
  locale: "pt",
  permissions: [],
  created_at: "2026-01-01T10:00:00Z",
  last_login_at: "2026-07-01T09:00:00Z",
}

const STATUS: McpStatus = {
  enabled: true,
  response_mode: "json",
  endpoint_path: "/api/mcp",
  has_permission: true,
  required_permission: "mcp.use",
  tools_count: 57,
  server_name: "centralops",
  server_version: "2.11.0",
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.getMyProfile.mockResolvedValue(USER)
})

describe("AccountSettingsPage — card MCP", () => {
  it("mostra servidor ligado, permissão presente e o snippet com a origem do navegador", async () => {
    mockedApi.getMcpStatus.mockResolvedValue(STATUS)
    render(<AccountSettingsPage />)

    await waitFor(() => expect(screen.getByTestId("account-mcp-status")).toBeInTheDocument())
    expect(screen.getByTestId("account-mcp-status")).toHaveTextContent("Servidor ligado")
    expect(screen.getByTestId("account-mcp-permission")).toHaveTextContent("Você pode usar o MCP")
    expect(screen.getByText("57 ferramentas disponíveis")).toBeInTheDocument()

    const snippet = JSON.parse(screen.getByTestId("account-mcp-snippet").textContent ?? "{}")
    expect(snippet.mcpServers.centralops.url).toBe(`${window.location.origin}/api/mcp`)
    expect(snippet.mcpServers.centralops.headers.Authorization).toBe("Bearer copsk_<api-key>")
    expect(screen.getByRole("link", { name: "Gerenciar tokens de API" })).toHaveAttribute("href", "/settings/tokens")
  })

  it("avisa quando o servidor está desligado e o papel não tem mcp.use", async () => {
    mockedApi.getMcpStatus.mockResolvedValue({ ...STATUS, enabled: false, has_permission: false })
    render(<AccountSettingsPage />)

    await waitFor(() => expect(screen.getByTestId("account-mcp-status")).toBeInTheDocument())
    expect(screen.getByTestId("account-mcp-status")).toHaveTextContent("Servidor desligado")
    expect(screen.getByTestId("account-mcp-permission")).toHaveTextContent("não inclui mcp.use")
  })

  it("falha do status não derruba a página: mostra o erro no card e o perfil continua", async () => {
    mockedApi.getMcpStatus.mockRejectedValue(new Error("boom"))
    render(<AccountSettingsPage />)

    await waitFor(() => expect(screen.getByText("boom")).toBeInTheDocument())
    expect(screen.getByText("alice")).toBeInTheDocument()
    expect(screen.queryByTestId("account-mcp-snippet")).not.toBeInTheDocument()
  })
})
