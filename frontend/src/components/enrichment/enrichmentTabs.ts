/**
 * Abas de `/enrichment` como ROTA. Cada aba tem endereço próprio
 * (`/enrichment/policies`, `/enrichment/sources`, ...) e a visão geral é a raiz.
 */

// Ordem por FREQUÊNCIA de uso, não pela ordem das tabelas do banco. A visão
// geral responde "está funcionando aqui?", que é a razão pela qual alguém abre
// esta tela; o catálogo é o primeiro passo de "nova fonte" e por isso deixou de
// ser a aba de entrada.
export const ENRICHMENT_TABS = [
  "overview",
  "policies",
  "sources",
  "tables",
  "catalog",
  "execution",
] as const
export type EnrichmentTab = (typeof ENRICHMENT_TABS)[number]

export function isEnrichmentTab(value: unknown): value is EnrichmentTab {
  return typeof value === "string" && (ENRICHMENT_TABS as readonly string[]).includes(value)
}

/** A visão geral é a raiz; as demais abas são `/enrichment/<aba>`. */
export function enrichmentTabPath(tab: EnrichmentTab): string {
  return tab === "overview" ? "/enrichment" : `/enrichment/${tab}`
}

/**
 * Aba de `/enrichment` que uma rota aponta, ou `null` se aponta para outra
 * página. Aceita `/enrichment/<aba>` e o formato antigo `/enrichment?tab=<aba>`.
 */
export function enrichmentTabFromRoute(route: string): EnrichmentTab | null {
  const [path, query = ""] = route.split("?", 2)
  if (path === "/enrichment") {
    const legacy = new URLSearchParams(query).get("tab")
    if (legacy === null) return "overview"
    return isEnrichmentTab(legacy) ? legacy : null
  }
  const m = /^\/enrichment\/([a-z]+)$/.exec(path)
  return m && isEnrichmentTab(m[1]) ? m[1] : null
}
