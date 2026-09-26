/**
 * Núcleo (puro, sem I/O) do guard de paridade/resolução de `t()` —
 * `scripts/check-i18n.mjs`. Extraído pra ser testável por vitest sem rodar o
 * script inteiro (que varre o repo todo e termina com `process.exit`) — ver
 * `src/__tests__/i18n-check-core.test.ts`.
 */

/** Recursively flatten a catalog to dotted keys; arrays count as one leaf. */
export function flatten(obj, prefix = "", out = new Set()) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === "object" && !Array.isArray(v)) flatten(v, key, out)
    else out.add(key)
  }
  return out
}

/**
 * `useTranslation("ns")` OU `useTranslation(["a", "b"])`. Com array, o `t`
 * devolvido é `getFixedT(lng, [a, b])` e o i18next resolve a chave sem
 * prefixo no PRIMEIRO namespace da lista que a contém — então a chave vale
 * se existir em QUALQUER um deles. Sem `useTranslation(...)` nenhum no
 * arquivo, cai no default do i18next: `"common"`.
 */
export function extractDefaultNamespaces(sourceText) {
  const nsMatch = sourceText.match(/useTranslation\(\s*(?:"([^"]+)"|\[([^\]]*)\])/)
  const defNs = nsMatch
    ? nsMatch[1]
      ? [nsMatch[1]]
      : [...nsMatch[2].matchAll(/"([^"]+)"/g)].map((x) => x[1])
    : ["common"]
  return defNs.length === 0 ? ["common"] : defNs
}

/**
 * Resolve `t("key")` (sem prefixo `ns:`) contra a lista de namespaces
 * candidatos — basta UM deles conter a chave (ou uma variante
 * plural/contexto `key_algumacoisa`, como o i18next resolve).
 *
 * `getCatalogKeys(ns)` devolve o `Set<string>` de chaves achatadas do
 * catálogo pt daquele namespace (ou `undefined`/vazio se o namespace não
 * existir) — desacoplado de disco pra ser testável com catálogos fake.
 */
export function keyResolvesInAnyNamespace(nsList, key, getCatalogKeys) {
  return nsList.some((ns) => {
    const set = getCatalogKeys(ns)
    if (!set || set.size === 0) return false
    if (set.has(key)) return true
    for (const k of set) if (k.startsWith(`${key}_`)) return true
    return false
  })
}

/**
 * Dado o texto bruto do t() (com ou sem prefixo `ns:chave`) e os namespaces
 * default do arquivo, devolve a lista de namespaces candidatos e a chave —
 * exatamente a decisão que `check-i18n.mjs` toma por chamada de `t()`.
 */
export function resolveNamespaceCandidates(raw, defaultNamespaces) {
  if (raw.includes(":")) {
    const [explicit, key] = raw.split(":", 2)
    return { nsList: [explicit], key }
  }
  return { nsList: defaultNamespaces, key: raw }
}
