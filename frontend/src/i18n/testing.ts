/**
 * Carga SÍNCRONA e completa de todos os catálogos — só para teste.
 *
 * O runtime de produção (`./index`) baixa preguiçosamente só o idioma
 * resolvido (PERF-01). Testes trocam de idioma e fazem a asserção de texto na
 * sequência, sem `await` (ex.: `i18n.changeLanguage("en")` seguido de
 * `getByText` na mesma função), e `lib/__tests__/labels.test.ts` lê
 * `resources[locale][ns]` dos 3 idiomas de uma vez, também sem `await`.
 * Nenhum dos dois sobrevive a um `import()` dinâmico no meio do caminho.
 *
 * Importe por efeito colateral, ANTES de qualquer `render()`:
 *   import "@/i18n/testing"
 *
 * (é isso que `src/test/setup.ts` faz — e por extensão
 * `web-ee/test/setup.ee.ts`, que importa o setup do Core.)
 */
import i18n, { __markLocaleLoadedForTests, resources } from "./index"

const catalogs = import.meta.glob<{ default: Record<string, unknown> }>("./locales/*/*.json", {
  eager: true,
})

const seenLocales = new Set<string>()
for (const path in catalogs) {
  const match = path.match(/\.\/locales\/([^/]+)\/(.+)\.json$/)
  if (!match) continue
  const [, locale, ns] = match
  const mod = catalogs[path]
  const data = (mod && "default" in mod ? mod.default : mod) as Record<string, unknown>
  ;(resources[locale] ??= {})[ns] = data
  i18n.addResourceBundle(locale, ns, data, true, true)
  seenLocales.add(locale)
}

// Sem isto, o `changeLanguage` patchado de `./index` veria o locale como "não
// carregado" e disparia um `import()` dinâmico redundante — que os testes que
// não dão `await` na troca de idioma não esperariam terminar a tempo.
for (const locale of seenLocales) __markLocaleLoadedForTests(locale)
