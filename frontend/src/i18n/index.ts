/**
 * i18n bootstrap (react-i18next) for the CentralOps platform SPA.
 *
 * Catalogs are auto-discovered from `src/i18n/locales/<locale>/<namespace>.json`
 * via import.meta.glob — so adding a NAMESPACE (a new screen's strings) or a whole
 * new LANGUAGE is a drop-in file with ZERO change to this module. That keeps the
 * screen-by-screen string extraction free of shared-file coordination.
 *
 * Locale codes are BASE codes (`pt`/`en`/`es`), not region-suffixed: base codes
 * resolve any navigator variant (pt-BR/pt-PT → pt, en-US → en) and sidestep an
 * i18next region-code translator quirk (getResource finds "pt-BR" but t() returns
 * the key). `pt` here IS Brazilian-Portuguese content, and is the fallback — so an
 * un-migrated / un-translated key resolves to Portuguese, never a raw key.
 *
 * PERF-01 — carga PREGUIÇOSA por idioma: os catálogos NÃO são embutidos no chunk
 * de entrada (o `import.meta.glob` abaixo é sem `eager`, então cada arquivo vira
 * um importador dinâmico). Só o idioma resolvido (localStorage > navegador >
 * <html lang>) baixa no boot — os outros dois só chegam se o usuário trocar de
 * idioma pelo LanguageSwitcher. Embutir os 3 (~545 kB brutos / 168 kB gzip)
 * custava esse peso no chunk de entrada para TODO usuário, mesmo quem nunca
 * troca de idioma.
 *
 * `i18nReady` resolve quando o idioma inicial terminou de carregar — `main.tsx`
 * aguarda antes do 1º `render()`, senão a tela pisca chave crua
 * (`common:actions.save`) até o catálogo chegar pela rede.
 *
 * `i18n.changeLanguage` é envolvido (ver `wrapChangeLanguage` abaixo) para
 * carregar o catálogo do idioma-alvo ANTES de trocar o idioma ativo — sem
 * isto, o `languageChanged` dispararia o re-render do app (react-i18next
 * escuta o evento) com o idioma novo ainda sem nenhum recurso carregado.
 *
 * EM TESTE: importe `./testing` (side-effect) — carrega os 3 catálogos de
 * forma síncrona (glob eager) e marca os 3 locales como já residentes, porque
 * testes trocam de idioma e fazem asserção de texto sem `await` a corrida do
 * `import()` dinâmico deste módulo.
 */
import i18n from "i18next"
import { initReactI18next } from "react-i18next"
import LanguageDetector from "i18next-browser-languagedetector"

export const SUPPORTED_LOCALES = ["pt", "en", "es"] as const
export type AppLocale = (typeof SUPPORTED_LOCALES)[number]

/** Persisted here; synced to the backend user profile later so API errors return
 *  in the same language (Fase 3/4). */
export const LOCALE_STORAGE_KEY = "centralops.locale"

// Um IMPORTADOR (não o módulo já resolvido) por arquivo
// `locales/<locale>/<namespace>.json` — carregar vira uma decisão de runtime,
// não um bundle fixo no chunk de entrada.
const catalogLoaders = import.meta.glob<{ default: Record<string, unknown> }>("./locales/*/*.json")

/**
 * Preenchido conforme os catálogos chegam — mesma forma de antes
 * (`resources[locale][ns]`), para não quebrar quem já lia daqui (ex.:
 * `lib/__tests__/labels.test.ts`), só que agora populado progressivamente em
 * produção (ver `loadLocale`) e de uma vez em teste (ver `./testing`).
 */
export const resources: Record<string, Record<string, unknown>> = {}

const nsSet = new Set<string>()
/** locale -> namespace -> importador do catálogo daquele par. */
const catalogsByLocale: Record<string, Record<string, () => Promise<{ default: Record<string, unknown> }>>> = {}
for (const path in catalogLoaders) {
  const match = path.match(/\.\/locales\/([^/]+)\/(.+)\.json$/)
  if (!match) continue
  const [, locale, ns] = match
  nsSet.add(ns)
  ;(catalogsByLocale[locale] ??= {})[ns] = catalogLoaders[path]
}
export const NAMESPACES = [...nsSet]

const loadedLocales = new Set<string>()

/**
 * Carrega (uma vez) todos os namespaces de um locale e injeta no i18next via
 * `addResourceBundle`. Idempotente: marca o locale como carregado ANTES do
 * `await`, então chamadas concorrentes (ex.: `i18nReady` e um clique rápido no
 * LanguageSwitcher) não disparam o fetch duas vezes.
 */
export async function loadLocale(locale: string): Promise<void> {
  if (loadedLocales.has(locale) || !catalogsByLocale[locale]) return
  loadedLocales.add(locale)
  await Promise.all(
    Object.entries(catalogsByLocale[locale]).map(async ([ns, load]) => {
      const mod = await load()
      const data = mod && "default" in mod ? mod.default : (mod as unknown as Record<string, unknown>)
      ;(resources[locale] ??= {})[ns] = data
      i18n.addResourceBundle(locale, ns, data, true, true)
    }),
  )
}

/** Só para `./testing`: marca um locale como já residente sem passar pelo
 *  `import()` dinâmico, para o `changeLanguage` patchado abaixo virar no-op
 *  (early-return síncrono) em vez de correr contra uma asserção sem `await`. */
export function __markLocaleLoadedForTests(locale: string): void {
  loadedLocales.add(locale)
}

// `i18n.changeLanguage` é a única porta de entrada usada pelo app inteiro
// (LanguageSwitcher, `test/setup.ts`, o próprio i18next durante o boot) — então
// interceptá-la aqui garante o "carrega antes de trocar" em qualquer chamador,
// sem precisar tocar em quem chama.
const baseChangeLanguage = i18n.changeLanguage.bind(i18n)
i18n.changeLanguage = ((lng?: string, callback?: Parameters<typeof baseChangeLanguage>[1]) => {
  // `lng` vem `undefined` na chamada interna que o próprio i18next faz durante
  // `init()` (a resolução do idioma inicial é feita DENTRO do `changeLanguage`
  // original, via o LanguageDetector) — nada para pré-carregar ainda, o
  // idioma resolvido só existe depois que essa chamada original terminar.
  if (!lng) return baseChangeLanguage(lng, callback)
  return loadLocale(lng).then(() => baseChangeLanguage(lng, callback))
}) as typeof baseChangeLanguage

const initPromise = i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    // Sem `resources` aqui: os catálogos chegam via `addResourceBundle`
    // (`loadLocale`). `partialBundledLanguages` deixa o i18next aceitar
    // `t()` mesmo com o idioma ativo parcialmente carregado, em vez de
    // reclamar de "idioma sem recursos" durante a janela de carregamento.
    partialBundledLanguages: true,
    fallbackLng: "pt",
    supportedLngs: SUPPORTED_LOCALES as unknown as string[],
    // Map navigator "en-US" / "es-419" → "en" / "es".
    nonExplicitSupportedLngs: true,
    ns: NAMESPACES,
    defaultNS: "common",
    interpolation: { escapeValue: false },
    returnNull: false,
    react: { useSuspense: false },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      lookupLocalStorage: LOCALE_STORAGE_KEY,
      caches: ["localStorage"],
    },
  })

/**
 * Resolve quando o catálogo do idioma INICIAL terminou de carregar.
 * `main.tsx` aguarda isto antes do 1º `render()`.
 */
export const i18nReady: Promise<void> = initPromise.then(async () => {
  const resolved = i18n.resolvedLanguage || "pt"
  await loadLocale(resolved)
  // Rede de segurança do fallback documentado acima ("nunca uma chave crua"):
  // só se cumpre se o catálogo pt estiver de fato carregado. Não bloqueia o
  // 1º render — dispara em paralelo; uma chave ausente no idioma resolvido
  // resolve para pt assim que chegar (react-i18next reage ao evento `added`
  // do i18next e re-renderiza os consumidores).
  if (resolved !== "pt") void loadLocale("pt")
})

// O <html lang> nasce fixo em "pt-BR" no index.html e nunca acompanhava o idioma
// resolvido: um usuário com navegador en-US lia a tela em inglês enquanto o
// documento se declarava português, e o leitor de tela aplicava fonética errada.
// `resolvedLanguage` (não `language`) porque o detector devolve "en-US" e o
// catálogo resolvido é "en".
const syncDocumentLang = () => {
  const lang = i18n.resolvedLanguage
  if (lang) document.documentElement.lang = lang
}
syncDocumentLang()
i18n.on("languageChanged", syncDocumentLang)

if (import.meta.env.DEV) {
  ;(window as unknown as { i18n?: typeof i18n }).i18n = i18n
}

export default i18n
