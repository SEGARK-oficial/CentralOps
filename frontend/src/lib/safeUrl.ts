/**
 * safeUrl — validação de URLs/paths controlados pelo usuário antes de usá-los
 * como `href`/`window.location`, para mitigar `javascript:`/`data:` XSS
 * (SEC-04) e open-redirect via path traversal (SEC-07).
 */

/**
 * safeExternalHref — aceita apenas URLs absolutas `http:`/`https:`.
 * Qualquer outro esquema (javascript:, data:, vbscript:, etc.), URL relativa
 * ou string inválida devolve `undefined` — o chamador deve tratar isso como
 * "sem link" (não renderizar o `<a href>`).
 */
export function safeExternalHref(u: string | null | undefined): string | undefined {
  if (!u) return undefined
  const trimmed = u.trim()
  if (!trimmed) return undefined

  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    return undefined
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return undefined
  return parsed.toString()
}

/**
 * safeInternalPath — aceita apenas paths internos relativos à origem atual.
 * Exige `/` inicial, rejeita `//` (protocol-relative → outro host) e rejeita
 * `\` (alguns navegadores tratam como `/`, contornando o check acima).
 * Devolve `undefined` quando o path não é seguro.
 */
export function safeInternalPath(p: string | null | undefined): string | undefined {
  if (!p) return undefined
  const trimmed = p.trim()
  if (!trimmed) return undefined

  if (!trimmed.startsWith("/")) return undefined
  if (trimmed.startsWith("//")) return undefined
  if (trimmed.includes("\\")) return undefined

  return trimmed
}
