/**
 * Snippet de configuração de cliente MCP.
 *
 * A URL é montada com a ORIGEM que o navegador está usando (o backend não
 * adivinha host nem esquema — atrás de proxy ele não sabe). O placeholder da
 * chave é deliberado: a PAT nasce em Tokens de API e é exibida uma única vez
 * lá; nenhuma tela reexibe uma chave existente.
 */

/** Neutro em idioma de propósito: o snippet é copiado para um arquivo de
 *  configuração, e a UI é servida em três línguas. A explicação é traduzida. */
export const MCP_KEY_PLACEHOLDER = "copsk_<api-key>"

export function mcpEndpointUrl(endpointPath: string, origin: string = window.location.origin): string {
  const path = endpointPath.startsWith("/") ? endpointPath : `/${endpointPath}`
  return `${origin.replace(/\/+$/, "")}${path}`
}

/** Formato `mcpServers` (Claude Desktop, Cursor, Windsurf, VS Code...). */
export function mcpServersSnippet(
  endpointPath: string,
  serverName = "centralops",
  key = MCP_KEY_PLACEHOLDER,
): string {
  const config = {
    mcpServers: {
      [serverName]: {
        url: mcpEndpointUrl(endpointPath),
        headers: { Authorization: `Bearer ${key}` },
      },
    },
  }
  return JSON.stringify(config, null, 2)
}

/** Linha de comando do Claude Code. */
export function claudeCodeAddCommand(
  endpointPath: string,
  serverName = "centralops",
  key = MCP_KEY_PLACEHOLDER,
): string {
  return `claude mcp add --transport http ${serverName} ${mcpEndpointUrl(endpointPath)} --header "Authorization: Bearer ${key}"`
}
