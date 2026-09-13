/**
 * McpConfigForm — aba "Assistentes (MCP)" da tela de configuração.
 *
 * Cobre: estado e contagem vindos do backend, o snippet montado com a ORIGEM
 * do navegador (o backend só devolve o caminho), o toggle + modo chegando ao
 * onSave como o backend espera, e o botão que só habilita quando algo mudou.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeAll, describe, expect, it, vi } from "vitest"

import { McpConfigForm } from "@/components/config/McpConfigForm"
import i18n from "@/i18n"
import type { McpConfig } from "@/types"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const CONFIG: McpConfig = {
  enabled: false,
  response_mode: "json",
  endpoint_path: "/api/mcp",
  tools_count: 57,
  is_persisted: false,
  updated_at: null,
}

function renderForm(overrides: Partial<McpConfig> = {}, onSave = vi.fn().mockResolvedValue(true)) {
  render(
    <McpConfigForm
      config={{ ...CONFIG, ...overrides }}
      loading={false}
      saving={false}
      feedback={null}
      onSave={onSave}
    />,
  )
  return onSave
}

describe("McpConfigForm", () => {
  it("mostra estado, contagem de ferramentas e o endpoint com a origem do navegador", () => {
    renderForm()
    expect(screen.getByTestId("mcp-status-badge")).toHaveTextContent("Desligado")
    expect(screen.getByTestId("mcp-tools-count")).toHaveTextContent("57")
    expect(screen.getByTestId("mcp-endpoint")).toHaveTextContent(`${window.location.origin}/api/mcp`)
  })

  it("monta o snippet mcpServers com a URL da instância e um placeholder de chave (nunca uma chave real)", () => {
    renderForm({ enabled: true })
    expect(screen.getByTestId("mcp-status-badge")).toHaveTextContent("Ligado")
    const snippet = JSON.parse(screen.getByTestId("mcp-snippet-json").textContent ?? "{}")
    expect(snippet.mcpServers.centralops.url).toBe(`${window.location.origin}/api/mcp`)
    // Placeholder neutro em idioma: a UI é trilíngue e o snippet vai para um arquivo.
    expect(snippet.mcpServers.centralops.headers.Authorization).toBe("Bearer copsk_<api-key>")
    expect(screen.getByTestId("mcp-snippet-cli").textContent).toContain("claude mcp add --transport http centralops")
  })

  it("só habilita Salvar quando algo mudou e envia enabled + response_mode", async () => {
    const onSave = renderForm()
    const save = screen.getByRole("button", { name: "Salvar configuração" })
    expect(save).toBeDisabled()

    // Único checkbox do formulário (o toggle de ativação).
    fireEvent.click(screen.getByRole("checkbox"))
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1))
    expect(onSave).toHaveBeenCalledWith({ enabled: true, response_mode: "json" })
  })

  it("mostra o feedback de erro vindo do hook", () => {
    render(
      <McpConfigForm
        config={CONFIG}
        loading={false}
        saving={false}
        feedback={{ type: "error", message: "Falha ao salvar a configuração do MCP" }}
        onSave={vi.fn()}
      />,
    )
    expect(screen.getByText("Falha ao salvar a configuração do MCP")).toBeInTheDocument()
  })
})
