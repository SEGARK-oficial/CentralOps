/**
 * Rótulos do enum `auth_status` da integração.
 *
 * O VALOR trocado com o backend continua em inglês; este módulo só diz o que
 * EXIBIR ao operador.
 *
 * O rótulo é CHAVE, não texto. Este módulo não tem contexto de React, então
 * texto fixo aqui saía em português com o resto da UI em inglês — era o que
 * acontecia em /integrations: a página toda em inglês e os selos de status em
 * português. Mesmo padrão do `labelKey` em `severity.tsx` e do
 * `BREAKER_LABEL_KEY` em DestinationHealthGrid: o mapa devolve a chave, o
 * componente resolve com `useTranslation`.
 *
 * As chaves vêm prefixadas com o namespace (`common:`) porque quem chama vive
 * em outro (`integrations`): sem o prefixo a chave resolveria contra o catálogo
 * errado e vazaria crua na tela. Elas reusam `common.states.*`, que já carrega
 * "Saudável"/"Degradado"/"Desconhecido" nos três idiomas — dois catálogos para
 * a mesma palavra é como uma tela passa a divergir da outra.
 *
 * Enum desconhecido cai de volta no valor cru: o i18next devolve a própria
 * chave quando não há entrada no catálogo, então um status novo do backend
 * chega inteiro ao operador em vez de virar um traço.
 *
 * Já houve aqui um SEVERITY_LABEL e um ASSET_STATUS_LABEL. Os dois morreram sem
 * call site (nenhum import em todo o repo) e guardavam mais treze strings PT-BR
 * fora do i18n. Traduzir código morto só encheria os três catálogos de chaves
 * que ninguém lê.
 */

type BadgeVariant = "success" | "warning" | "danger" | "primary" | "outline" | "default"

const AUTH_STATUS_LABEL_KEY: Record<string, string> = {
  healthy: "common:states.healthy",
  degraded: "common:states.degraded",
  error: "common:states.error",
  unknown: "common:states.unknown",
}

// Credencial saudável é estado de repouso, e repouso é neutro: numa lista com
// dezenas de integrações no ar, matiz em "continua ok" afoga o degradado. Só o
// que quebrou sozinho ganha cor.
const AUTH_STATUS_VARIANT: Record<string, BadgeVariant> = {
  healthy: "default",
  degraded: "warning",
  error: "danger",
  unknown: "outline",
}

/** Chave i18n do status de autenticação. Resolva com `t()` no componente. */
export function authStatusLabelKey(status?: string | null): string {
  if (!status) return AUTH_STATUS_LABEL_KEY.unknown
  return AUTH_STATUS_LABEL_KEY[status.toLowerCase()] ?? status
}

export function authStatusVariant(status?: string | null): BadgeVariant {
  if (!status) return "outline"
  return AUTH_STATUS_VARIANT[status.toLowerCase()] ?? "default"
}

// Exportado para o teste amarrar toda chave aos três catálogos.
export { AUTH_STATUS_LABEL_KEY }
