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
 * idioma pelo LanguageSwitcher.
 *
 * R2-5.1 — carga PREGUIÇOSA por NAMESPACE também: o boot só embute
 * `SHELL_NAMESPACES` (o que o shell da app — layout, primitivos de UI, login —
 * usa antes de qualquer rota decidir o que mostrar). Os outros ~15 namespaces
 * (por tela: `mappings`, `correlation` só-EE, etc.) chegam sob demanda via um
 * backend i18next local (`shellBackend` abaixo, sobre o mesmo
 * `import.meta.glob`, sem dependência nova) quando um componente chama
 * `useTranslation(ns)` pela 1ª vez — com Suspense, capturado pelo
 * `<Suspense>` que o `AppLayout` já usa nas rotas lazy (o chunk da rota e o
 * catálogo da tela resolvem juntos). Antes, o boot baixava os 20 namespaces
 * do idioma inteiro (238 kB / 65 kB gzip) de uma vez, incluindo `correlation`
 * (só o EE usa) no bundle do CE.
 *
 * `i18nReady` resolve quando o SHELL do idioma inicial terminou de carregar —
 * `main.tsx` aguarda antes do 1º `render()`, senão a tela pisca chave crua
 * (`common:actions.save`) até o catálogo chegar pela rede.
 *
 * `i18n.changeLanguage` é envolvido (ver `wrapChangeLanguage` abaixo) para
 * carregar o SHELL do idioma-alvo ANTES de trocar o idioma ativo — sem isto,
 * o `languageChanged` dispararia o re-render do app (react-i18next escuta o
 * evento) com o idioma novo ainda sem nenhum recurso carregado. Os namespaces
 * não-shell da tela atual, se ainda não carregados no novo idioma, suspendem
 * de novo — mesmo caminho do backend.
 *
 * EM TESTE: importe `./testing` (side-effect) — carrega TODOS os catálogos de
 * forma síncrona (glob eager) e marca os 3 locales como já residentes, porque
 * testes trocam de idioma e fazem asserção de texto sem `await` a corrida do
 * `import()` dinâmico deste módulo. Com tudo já carregado, `hasLoadedNamespace`
 * é sempre `true` em teste — o `useSuspense: true` abaixo nunca chega a
 * suspender lá.
 */
import i18n from "i18next"
import type { BackendModule, ReadCallback } from "i18next"
import { initReactI18next } from "react-i18next"
import LanguageDetector from "i18next-browser-languagedetector"

export const SUPPORTED_LOCALES = ["pt", "en", "es"] as const
export type AppLocale = (typeof SUPPORTED_LOCALES)[number]

/** Persisted here; synced to the backend user profile later so API errors return
 *  in the same language (Fase 3/4). */
export const LOCALE_STORAGE_KEY = "centralops.locale"

/**
 * Namespaces do SHELL — carregados eager (via `addResourceBundle`) para o
 * idioma resolvido, ANTES do 1º render:
 *  - `common`/`ui`: primitivos usados em quase toda tela (Button, Select,
 *    DataTable, Modal, LoadingSpinner…) — suspenderiam em cascata se fossem
 *    sob demanda.
 *  - `nav`: `AppLayout`/`Header`/`Navigation`/`Breadcrumbs`/`GlobalFilters`/
 *    `UserMenu`, e também `AppLoadingScreen`/`AppCommandPalette` (`App.tsx`).
 *  - `auth`: `LoginPage` renderiza FORA do `<Suspense>` do shell (é a única
 *    rota que existe antes de qualquer coisa "carregar") — sem o namespace
 *    já residente, o 1º acesso suspenderia sem nenhum Suspense ancestral
 *    para capturar, e a tela de login ficaria em branco.
 */
const SHELL_NAMESPACES = ["common", "nav", "ui", "auth"] as const

// Um IMPORTADOR (não o módulo já resolvido) por arquivo
// `locales/<locale>/<namespace>.json` — carregar vira uma decisão de runtime,
// não um bundle fixo no chunk de entrada.
const catalogLoaders = import.meta.glob<{ default: Record<string, unknown> }>("./locales/*/*.json")

/**
 * Preenchido conforme os catálogos chegam — mesma forma de antes
 * (`resources[locale][ns]`), para não quebrar quem já lia daqui (ex.:
 * `lib/__tests__/labels.test.ts`), só que agora populado progressivamente em
 * produção (shell via `loadShellNamespaces`, o resto via `shellBackend.read`)
 * e de uma vez em teste (ver `./testing`).
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
 * Carrega (uma vez) os `SHELL_NAMESPACES` de um locale e injeta no i18next via
 * `addResourceBundle`. Idempotente: marca o locale como carregado ANTES do
 * `await`, então chamadas concorrentes (ex.: `i18nReady` e um clique rápido no
 * LanguageSwitcher) não disparam o fetch duas vezes. Os demais namespaces
 * chegam sob demanda via `shellBackend` (definido abaixo), acionado pelo
 * primeiro `useTranslation(ns)` de cada tela.
 */
export async function loadLocale(locale: string): Promise<void> {
  if (loadedLocales.has(locale) || !catalogsByLocale[locale]) return
  loadedLocales.add(locale)
  await Promise.all(
    SHELL_NAMESPACES.map(async (ns) => {
      const load = catalogsByLocale[locale][ns]
      if (!load) return
      const mod = await load()
      const data = mod && "default" in mod ? mod.default : (mod as unknown as Record<string, unknown>)
      ;(resources[locale] ??= {})[ns] = data
      i18n.addResourceBundle(locale, ns, data, true, true)
    }),
  )
}

/**
 * Backend i18next local (sem dependência nova, no estilo do
 * `i18next-resources-to-backend`): resolve QUALQUER par idioma/namespace que
 * ainda não esteja em `resources` puxando do MESMO `import.meta.glob` acima.
 * `react-i18next` (com `useSuspense: true`) chama isto automaticamente — via
 * `i18n.loadNamespaces` — na 1ª vez que um componente usa um namespace fora
 * do shell, e resolve o fallback (`pt`) por namespace do mesmo jeito, sem
 * baixar o idioma inteiro para isso.
 */
const shellBackend: BackendModule = {
  type: "backend",
  init() {},
  read(language: string, namespace: string, callback: ReadCallback) {
    const load = catalogsByLocale[language]?.[namespace]
    if (!load) {
      // Par sem catálogo (ex.: namespace só-EE ausente no locale, ou locale
      // desconhecido): devolve vazio em vez de erro — i18next trata como
      // "carregado, sem chaves" e o fallbackLng segue resolvendo o resto.
      callback(null, {})
      return
    }
    load()
      .then((mod) => {
        const data = mod && "default" in mod ? mod.default : (mod as unknown as Record<string, unknown>)
        ;(resources[language] ??= {})[namespace] = data
        callback(null, data)
      })
      .catch((err: unknown) => callback(err instanceof Error ? err : String(err), undefined))
  },
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
  .use(shellBackend)
  .use(initReactI18next)
  .init({
    // Sem `resources` aqui: o SHELL chega via `addResourceBundle`
    // (`loadLocale`), o resto via `shellBackend.read`. `partialBundledLanguages`
    // é exatamente para essa mistura — deixa o i18next aceitar `t()` com o
    // idioma parcialmente carregado (shell) e usar o backend para completar o
    // resto sob demanda, em vez de reclamar de "idioma sem recursos".
    partialBundledLanguages: true,
    fallbackLng: "pt",
    supportedLngs: SUPPORTED_LOCALES as unknown as string[],
    // Map navigator "en-US" / "es-419" → "en" / "es".
    nonExplicitSupportedLngs: true,
    // R2-5.1: só o SHELL aqui — não `NAMESPACES` (todos). Com um backend
    // registrado, `i18next.init()`/`loadResources()` baixa TODO namespace
    // listado em `ns` no boot via `backendConnector.load`, mesmo sem nenhum
    // componente ter pedido — declarar os ~20 aqui reintroduziria o boot
    // gigante que este achado elimina. Os demais entram um a um via
    // `i18n.loadNamespaces()` (chamado pelo `useTranslation(ns)` do
    // react-i18next), que ACRESCENTA ao `ns` interno sob demanda.
    ns: SHELL_NAMESPACES as unknown as string[],
    defaultNS: "common",
    interpolation: { escapeValue: false },
    returnNull: false,
    // R2-5.1: Suspense por namespace. Em teste, `./testing` já deixou tudo
    // carregado antes do 1º render (ver docstring do topo) — `ready` já
    // nasce `true`, então o `throw` do react-i18next nunca é alcançado lá.
    react: { useSuspense: true },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      lookupLocalStorage: LOCALE_STORAGE_KEY,
      caches: ["localStorage"],
    },
  })

/**
 * Resolve quando o SHELL do idioma INICIAL terminou de carregar.
 * `main.tsx` aguarda isto antes do 1º `render()`. Namespaces de tela (fora do
 * shell) carregam depois, sob demanda, via Suspense.
 */
export const i18nReady: Promise<void> = initPromise.then(async () => {
  const resolved = i18n.resolvedLanguage || "pt"
  await loadLocale(resolved)
  // Rede de segurança do fallback documentado acima ("nunca uma chave crua"):
  // só se cumpre se o SHELL em pt estiver de fato carregado. Não bloqueia o
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
