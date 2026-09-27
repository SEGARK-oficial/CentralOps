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
 * `<Suspense>` que o `AppLayout` já usa nas rotas lazy.
 *
 * R3-5.1 — regressão da R2-5.1: um componente de OUTRO namespace montado
 * DEPOIS do 1º render (ex.: abre um Modal com uma tela que usa `destinations`)
 * suspende até o único `<Suspense>` da rota — a página inteira some. Três
 * mitigações, todas nesta camada (a Sub 8 também põe Suspense LOCAL em
 * Modal/Drawer/Tabs como rede de segurança, em paralelo):
 *   (a) `schedulePrefetch` — depois do 1º render, em idle
 *       (`requestIdleCallback`, com fallback `setTimeout`), busca os
 *       namespaces RESTANTES do idioma ativo. Não bloqueia nada; em poucos
 *       segundos, o catálogo inteiro já está em memória e o Suspense tardio
 *       vira um caso raro (corrida perdida contra o idle), não o normal.
 *   (b) `read()` não busca mais o fallback `pt` para um namespace de TELA
 *       quando o idioma ativo não é `pt` — o `check-i18n` (CI) garante
 *       paridade total de chaves entre os 3 locales para TODO namespace, então
 *       esse fallback nunca tinha o que cobrir na prática, e cada namespace de
 *       tela baixava em DOBRO para usuários en/es (o ativo + o pt "de
 *       segurança", nunca lido). O SHELL continua com o fallback pt de
 *       verdade (já está em memória de qualquer forma, carregado no boot).
 *   (c) o fetch do SHELL do idioma provável começa em PARALELO com o
 *       `i18n.init()` (chute síncrono pela mesma ordem do LanguageDetector:
 *       localStorage > navegador), em vez de esperar o `init()` terminar pra
 *       só então descobrir o idioma e começar a baixar — sem isso, o boot
 *       tinha uma cascata (JS de entrada → init → só aí o 1º `import()` do
 *       JSON). Se o chute errar (raro), o `loadLocale` de dentro do
 *       `i18nReady` (idioma OFICIAL, pós-detector) corrige sozinho.
 *
 * R4-5.1 — regressão da R3-5.1(b): quem trocava de en/es para pt via
 * `changeLanguage` via LanguageSwitcher via chave crua. Cadeia do bug:
 *   1. com en/es ativo, o 1º `useTranslation(ns)` de uma tela faz o i18next
 *      pedir TAMBÉM `pt|ns` (fallbackLng sempre entra na hierarquia — ver
 *      R3-5.1(b) acima).
 *   2. o `read()` da R3-5.1(b) respondia `callback(null, {})` pra esse passe
 *      redundante — um objeto VAZIO, mas ainda assim "dados" pro i18next
 *      (`!err && data` com `data={}` é verdadeiro, `{}` é truthy em JS). O
 *      `backendConnector` então CHAMAVA `store.addResourceBundle(pt, ns, {})`
 *      de verdade — um bundle vazio, mas RESULTAT sim, com estado 2
 *      ("carregado com sucesso").
 *   3. ao trocar pra pt, o `backendConnector.prepareLoading` via
 *      `hasResourceBundle(pt, ns)` — TRUE (o bundle vazio existe) — e PULA a
 *      carga de verdade. `t()` não acha a chave no bundle vazio e devolve a
 *      chave crua.
 *   4. mesmo com o prefetch de idle injetando os dados de verdade depois (via
 *      `addResourceBundle` direto, sem passar pelo `backendConnector`),
 *      react-i18next não re-renderizava: o `bindI18nStore` padrão é `''`
 *      (react-i18next não escuta o evento `added` da store por padrão).
 * Correção (duas partes, as duas necessárias):
 *   - `read()` não grava mais bundle nenhum pro passe redundante — responde
 *     com um ERRO de verdade (`callback(err, false)`), que deixa o
 *     `backendConnector` em estado -1 (falha) pra aquele par, SEM nunca
 *     chamar `addResourceBundle`. `hasResourceBundle(pt, ns)` continua FALSE
 *     até alguém carregar de verdade.
 *   - o `changeLanguage` patchado (abaixo) carrega de VERDADE, via
 *     `ensureNamespaceLoaded` (que não passa pelo `backendConnector`, então
 *     nem liga pro estado -1 dele), TODO namespace que a app já usou
 *     (`i18n.reportNamespaces.getUsedNamespaces()`) pro idioma-ALVO, ANTES de
 *     trocar — sem isto, o estado -1 do passo acima bloquearia pra sempre
 *     uma nova tentativa automática via Suspense (o `backendConnector` só
 *     tenta de novo um par com estado negativo se alguém pedir com
 *     `reload:true`, o que o fluxo normal nunca faz).
 *   - `react.bindI18nStore: "added"` — rede de segurança geral: qualquer
 *     bundle que chegue DEPOIS (prefetch, o pré-carregamento acima, um
 *     `useTranslation` concorrente) agora dispara um re-render de quem já
 *     estava montado, em vez de deixar texto obsoleto na tela até o próximo
 *     re-render por outro motivo.
 *
 * `i18nReady` resolve quando o SHELL do idioma inicial terminou de carregar —
 * `main.tsx` aguarda antes do 1º `render()`, senão a tela pisca chave crua
 * (`common:actions.save`) até o catálogo chegar pela rede.
 *
 * `i18n.changeLanguage` é envolvido (ver `baseChangeLanguage` abaixo) para
 * carregar o SHELL do idioma-alvo ANTES de trocar o idioma ativo — sem isto,
 * o `languageChanged` dispararia o re-render do app (react-i18next escuta o
 * evento) com o idioma novo ainda sem nenhum recurso carregado. Os namespaces
 * não-shell da tela atual, se ainda não carregados no novo idioma, suspendem
 * de novo — mesmo caminho do backend (e o prefetch de idle roda de novo para
 * o novo idioma).
 *
 * EM TESTE: importe `./testing` (side-effect) — carrega TODOS os catálogos de
 * forma síncrona (glob eager) e marca os 3 locales (E todos os pares
 * locale/namespace) como já residentes, porque testes trocam de idioma e
 * fazem asserção de texto sem `await` a corrida do `import()` dinâmico deste
 * módulo. Com tudo já carregado, `hasLoadedNamespace` é sempre `true` em
 * teste — o `useSuspense: true` abaixo nunca chega a suspender lá, e o
 * `schedulePrefetch`/`read()` viram no-op (nada para buscar).
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
 * produção (`ensureNamespaceLoaded`) e de uma vez em teste (ver `./testing`).
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
/** chave `${locale}::${ns}` — idempotência por PAR, compartilhada entre
 *  `loadLocale` (shell), `shellBackend.read` (sob demanda) e
 *  `schedulePrefetch` (idle), pra nenhum dos três refazer o fetch/`import()`
 *  de um par que outro já resolveu. */
const loadedNamespaceKeys = new Set<string>()

/** Fetch EM VOO por par `locale::ns` — dedup de chamadas concorrentes de
 *  verdade (ver achado abaixo em `ensureNamespaceLoaded`), não só "já
 *  começou". */
const loadingPromises = new Map<string, Promise<Record<string, unknown> | null>>()

/** Carrega (uma vez) um par locale/namespace e injeta via `addResourceBundle`.
 *  `null` quando não há catálogo pra esse par (ex.: namespace só-EE ausente
 *  no locale).
 *
 * Achado real (investigado nesta rodada, via flake em teste): a versão
 * anterior marcava `loadedNamespaceKeys.add(key)` ANTES do `await load()` —
 * a intenção era so dedupe de chamadas concorrentes (shell + prefetch de
 * idle + um `useTranslation` que cai no meio), mas o CHECK de cache-hit no
 * topo (`loadedNamespaceKeys.has(key)`) usa o MESMO Set pra "já terminou de
 * carregar" — uma 2ª chamada concorrente, caindo DEPOIS do `add` mas ANTES do
 * dado real ser gravado em `resources[locale][ns]`, via `hasResourceBundle`
 * o via cache-hit, devolvia `null` (não o dado real, que ainda não existia).
 * Se essa 2ª chamada fosse a que o `shellBackend.read()` de um `useTranslation`
 * realmente usava pra resolver o Suspense, o namespace ficava "pronto" com
 * bundle VAZIO — chave crua, exatamente a classe de regressão que este achado
 * (R4-5.1) existe pra evitar, só que via uma porta diferente (corrida entre o
 * prefetch de idle e o carregamento sob demanda do MESMO par, não entre
 * idiomas). Corrigido com um Map de PROMISES em voo: toda chamada concorrente
 * pro MESMO par aguarda a MESMA promise (o dado real), nunca um placeholder. */
async function ensureNamespaceLoaded(locale: string, ns: string): Promise<Record<string, unknown> | null> {
  const key = `${locale}::${ns}`
  if (loadedNamespaceKeys.has(key)) return (resources[locale]?.[ns] as Record<string, unknown> | undefined) ?? null
  const inFlight = loadingPromises.get(key)
  if (inFlight) return inFlight
  const load = catalogsByLocale[locale]?.[ns]
  if (!load) return null
  const promise = (async () => {
    try {
      const mod = await load()
      const data = mod && "default" in mod ? mod.default : (mod as unknown as Record<string, unknown>)
      ;(resources[locale] ??= {})[ns] = data
      i18n.addResourceBundle(locale, ns, data, true, true)
      loadedNamespaceKeys.add(key)
      return data
    } finally {
      loadingPromises.delete(key)
    }
  })()
  loadingPromises.set(key, promise)
  return promise
}

/**
 * Carrega (uma vez) os `SHELL_NAMESPACES` de um locale. Os demais namespaces
 * chegam sob demanda via `shellBackend` (definido abaixo), acionado pelo
 * primeiro `useTranslation(ns)` de cada tela, ou pelo prefetch de idle
 * (`schedulePrefetch`).
 */
export async function loadLocale(locale: string): Promise<void> {
  if (loadedLocales.has(locale) || !catalogsByLocale[locale]) return
  loadedLocales.add(locale)
  await Promise.all(SHELL_NAMESPACES.map((ns) => ensureNamespaceLoaded(locale, ns)))
}

/**
 * Backend i18next local (sem dependência nova, no estilo do
 * `i18next-resources-to-backend`): resolve QUALQUER par idioma/namespace que
 * ainda não esteja em `resources` puxando do MESMO `import.meta.glob` acima.
 * `react-i18next` (com `useSuspense: true`) chama isto automaticamente — via
 * `i18n.loadNamespaces` — na 1ª vez que um componente usa um namespace fora
 * do shell.
 *
 * R3-5.1(b): i18next SEMPRE inclui o `fallbackLng` na hierarquia de busca
 * (`toResolveHierarchy` — não tem como desligar isso só com opções de
 * `init()`), então sem este corte, todo namespace de TELA baixava em dobro
 * pra quem usa en/es: o idioma ativo + "pt de segurança", que o
 * `check-i18n` (CI) garante que nunca precisa ser lido (paridade de chaves
 * é total). O corte só vale pra namespace de TELA — o SHELL usa o fallback
 * pt de verdade, e já está em memória de qualquer jeito.
 */
const shellBackend: BackendModule = {
  type: "backend",
  init() {},
  read(language: string, namespace: string, callback: ReadCallback) {
    const isShellNs = (SHELL_NAMESPACES as readonly string[]).includes(namespace)
    const activeLanguage = i18n.resolvedLanguage || i18n.language
    const isRedundantFallbackPass = language === "pt" && language !== activeLanguage && !isShellNs
    if (isRedundantFallbackPass) {
      // R4-5.1: NUNCA `callback(null, {})` aqui — um objeto vazio ainda é
      // "dado" pro i18next (`{}` é truthy), então o `backendConnector`
      // gravaria um bundle VAZIO com estado 2 ("carregado"). Depois, ao
      // trocar de verdade para pt, `hasResourceBundle(pt, ns)` já seria
      // `true` e a carga real nunca aconteceria — chave crua pra sempre
      // (a regressão que este achado corrige). Erro de verdade deixa o
      // `backendConnector` em estado -1 (falha) SEM tocar a store: quem
      // garante a carga real de pt, quando for a vez de pt, é o
      // `changeLanguage` patchado abaixo (via `ensureNamespaceLoaded`, que
      // ignora esse estado por completo).
      callback(new Error("R4-5.1: passe de fallback pt redundante, ignorado de propósito"), false)
      return
    }
    ensureNamespaceLoaded(language, namespace)
      .then((data) => callback(null, data ?? {}))
      .catch((err: unknown) => callback(err instanceof Error ? err : String(err), undefined))
  },
}

/**
 * R4-5.2: namespaces que só existem para telas do overlay EE
 * (`web-ee/**`) — NENHUM módulo do bundle CE os usa. `correlation` é hoje o
 * único (confirmado empiricamente: `grep useTranslation("correlation"` só dá
 * resultado em `web-ee/`; todo outro namespace tem pelo menos 1 uso em
 * `src/`).
 *
 * IMPORTANTE (regressão já cometida e corrigida — não repetir): este módulo
 * NÃO IMPORTA nada de `@/ee/*` nem de páginas para descobrir "isto é EE?". Uma
 * 1ª versão fazia `import { eeRoutes } from "@/ee/routes"` aqui — como
 * `src/i18n/index.ts` é avaliado no BOOT (via `src/test/setup.ts` →
 * `@/i18n/testing` → `./index`), ANTES de qualquer `vi.mock` de teste se
 * aplicar e antes de qualquer Provider real existir, isso puxava o overlay EE
 * inteiro (páginas, `usePermission`, `AuthContext` REAL) pra dentro da cadeia
 * de import do i18n. Resultado: 34 testes EE quebraram com "useAuth must be
 * used within an AuthProvider" (o `AuthContext` real, não o mockado, já tinha
 * rodado). Em produção, criava um ciclo rotas→páginas→i18n indesejado — o
 * i18n é a camada MAIS a montante do boot, não deveria depender de nada
 * feature-específico, muito menos de EE.
 *
 * Correção: a dependência é INVERTIDA. Por padrão (build CE, ou mesmo build
 * EE antes de qualquer rota se registrar), `EE_ONLY_NAMESPACES` fica de fora
 * do prefetch. Quem SABE que é EE — o overlay `web-ee/routes.tsx`, no nível
 * do MÓDULO (roda na 1ª vez que é importado, ou seja, só quando o bundle EE
 * de verdade existe) — chama `allowPrefetchNamespaces(["correlation"])` pra
 * liberar. O i18n nunca enxerga `@/ee/*`; a informação flui só EE → i18n.
 */
const EE_ONLY_NAMESPACES: readonly string[] = ["correlation"]

/** Liberado via `allowPrefetchNamespaces` — vazio por padrão (CE, ou EE antes
 *  do overlay se registrar). */
const allowedPrefetchNamespaces = new Set<string>()

/**
 * Chamada pelo overlay EE (`web-ee/routes.tsx`, no nível do módulo — nunca
 * dentro de um componente) para liberar namespaces de `EE_ONLY_NAMESPACES` no
 * prefetch de idle. Idempotente; seguro de chamar antes OU depois do boot —
 * só afeta o que o PRÓXIMO `schedulePrefetch` busca. Não afeta o carregamento
 * sob demanda via Suspense: uma tela EE que usa `correlation` carrega esse
 * namespace normalmente de qualquer jeito (`shellBackend.read` não filtra por
 * `EE_ONLY_NAMESPACES` — o filtro é só do prefetch OPORTUNISTA de idle).
 */
export function allowPrefetchNamespaces(namespaces: readonly string[]): void {
  for (const ns of namespaces) allowedPrefetchNamespaces.add(ns)
}

/**
 * R3-5.1(a): depois do boot, em idle, busca os namespaces RESTANTES do
 * idioma ativo (todos os que `NAMESPACES` conhece e ainda não carregaram).
 * Não é awaited por ninguém — só reduz a JANELA em que um componente de tela
 * ainda pode suspender até o `<Suspense>` da rota inteira.
 *
 * R4-5.2: pula `EE_ONLY_NAMESPACES` a menos que `allowPrefetchNamespaces` já
 * tenha liberado (ver docstring acima) — antes, o prefetch baixava TODO
 * namespace, `correlation` incluso, mesmo numa instância CE onde nenhuma tela
 * jamais vai pedir esse JSON (~61 KB gzip de todos os namespaces, com
 * `correlation` sendo uma fatia relevante disso, desperdiçada em toda sessão
 * CE).
 */
function prefetchRemainingNamespaces(locale: string): void {
  for (const ns of NAMESPACES) {
    if (EE_ONLY_NAMESPACES.includes(ns) && !allowedPrefetchNamespaces.has(ns)) continue
    void ensureNamespaceLoaded(locale, ns)
  }
}

function schedulePrefetch(locale: string): void {
  const run = () => prefetchRemainingNamespaces(locale)
  const w = window as unknown as { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number }
  if (typeof w.requestIdleCallback === "function") {
    w.requestIdleCallback(run, { timeout: 5000 })
  } else {
    setTimeout(run, 0)
  }
}

/** Só para `./testing`: marca um locale — e TODOS os seus pares
 *  locale/namespace — como já residentes, sem passar pelo `import()`
 *  dinâmico. Sem isto, o `changeLanguage` patchado veria o locale como "não
 *  carregado" (e disparia um `import()` redundante que testes sem `await` não
 *  esperariam a tempo), e o `schedulePrefetch`/`read()` tentariam refazer o
 *  fetch de namespaces que `./testing` já injetou via glob eager. */
export function __markLocaleLoadedForTests(locale: string): void {
  loadedLocales.add(locale)
  for (const ns of Object.keys(catalogsByLocale[locale] ?? {})) {
    loadedNamespaceKeys.add(`${locale}::${ns}`)
  }
}

// `i18n.changeLanguage` é a única porta de entrada usada pelo app inteiro
// (LanguageSwitcher, `test/setup.ts`, o próprio i18next durante o boot) — então
// interceptá-la aqui garante o "carrega antes de trocar" em qualquer chamador,
// sem precisar tocar em quem chama.
/** Forma mínima do que `react-i18next` pendura em `i18n.reportNamespaces` —
 *  não faz parte dos tipos do i18next (é uma extensão do react-i18next),
 *  então o acesso abaixo é via este shape explícito, não um `any`. Pode não
 *  existir ainda (só nasce no 1º `useTranslation()` da app) — daí o `?.`. */
interface ReportNamespacesLike {
  getUsedNamespaces(): string[]
}

const baseChangeLanguage = i18n.changeLanguage.bind(i18n)
i18n.changeLanguage = ((lng?: string, callback?: Parameters<typeof baseChangeLanguage>[1]) => {
  // `lng` vem `undefined` na chamada interna que o próprio i18next faz durante
  // `init()` (a resolução do idioma inicial é feita DENTRO do `changeLanguage`
  // original, via o LanguageDetector) — nada para pré-carregar ainda, o
  // idioma resolvido só existe depois que essa chamada original terminar.
  if (!lng) return baseChangeLanguage(lng, callback)
  return loadLocale(lng).then(async () => {
    // R4-5.1: carrega de VERDADE, para o idioma-ALVO, todo namespace que a
    // app já usou em QUALQUER idioma — via `ensureNamespaceLoaded` (que
    // ignora por completo o estado -1/2 do `backendConnector`, chamando
    // `addResourceBundle` direto). Isto acontece ANTES da troca de idioma de
    // fato (linha de baixo), pra garantir que `hasResourceBundle` já seja
    // `true` — com dado REAL — no instante em que qualquer componente montado
    // re-renderiza no idioma novo. Sem isto, um namespace que já tinha
    // recebido o passe de fallback redundante (rejeitado logo acima, em
    // `shellBackend.read`) ficaria PARA SEMPRE em estado -1 no
    // `backendConnector` — que só tenta de novo automaticamente com
    // `reload: true`, o que o fluxo normal (Suspense/`useTranslation`) nunca
    // pede.
    const reportNamespaces = (i18n as unknown as { reportNamespaces?: ReportNamespacesLike }).reportNamespaces
    const usedNamespaces = reportNamespaces?.getUsedNamespaces() ?? []
    await Promise.all(usedNamespaces.map((ns) => ensureNamespaceLoaded(lng, ns)))
    const result = baseChangeLanguage(lng, callback)
    schedulePrefetch(lng)
    return result
  })
}) as typeof baseChangeLanguage

/**
 * R3-5.1(c): chute SÍNCRONO do idioma provável, pela MESMA ordem do
 * `LanguageDetector` abaixo (localStorage > navegador) — dispara o `import()`
 * do SHELL em PARALELO com `i18n.init()`, em vez de esperar o `init()`
 * terminar (e só então descobrir o idioma) pra começar a buscar. Se o chute
 * errar (raríssimo — normalmente é a mesma lógica), `i18nReady` corrige com
 * o idioma OFICIAL (pós-detector); `loadLocale` é idempotente, então o chute
 * certo vira um no-op ali, sem custo.
 */
function guessInitialLocale(): string {
  try {
    const stored = window.localStorage?.getItem(LOCALE_STORAGE_KEY)
    if (stored) {
      const base = stored.toLowerCase().split(/[-_]/)[0]
      if ((SUPPORTED_LOCALES as readonly string[]).includes(base)) return base
    }
  } catch {
    // localStorage bloqueado (modo privado restrito, política de origem) —
    // segue para o navegador.
  }
  const nav = (navigator.languages?.[0] || navigator.language || "pt").toLowerCase()
  const base = nav.split(/[-_]/)[0]
  return (SUPPORTED_LOCALES as readonly string[]).includes(base) ? base : "pt"
}
// Em teste, `./testing` (glob EAGER, síncrono) já deixa tudo carregado antes
// de qualquer asserção — disparar um `import()` dinâmico aqui, na AVALIAÇÃO
// do módulo (antes de `./testing` rodar a própria carga, mais abaixo na
// mesma cadeia de import), competiria com ela à toa em CADA arquivo de
// teste, sem nenhum efeito observável (idempotente) além de trabalho e
// promises soltas que nenhum teste aguarda.
if (import.meta.env.MODE !== "test") void loadLocale(guessInitialLocale())

/**
 * R4-5.5 (avaliado, NÃO implementado — decisão documentada):
 *
 * Mesmo com o chute síncrono acima (R3-5.1(c)), os 4 chunks do SHELL
 * (`common`/`nav`/`ui`/`auth` do locale resolvido) são `import()` DINÂMICOS
 * — o Vite só auto-injeta `<link rel="modulepreload">` no `index.html` pro
 * grafo de imports ESTÁTICOS alcançável a partir do entry (`main.tsx` →
 * `./i18n` → i18next/react-i18next/detector, tudo isso JÁ vem com preload de
 * graça). Os 4 chunks de locale só entram no radar do navegador quando a
 * AVALIAÇÃO deste módulo chega literalmente nesta linha — ou seja, só depois
 * que o grafo estático inteiro já foi baixado E parseado. Um
 * `<link rel="modulepreload">` apontando pros 4 chunks do shell `pt`,
 * injetado direto no HTML inicial, deixaria o navegador buscá-los em
 * PARALELO com o grafo estático em vez de em série — 1 round-trip a menos,
 * só em cache FRIO (num retorno com o HTTP cache quente, os chunks já
 * residem localmente e o ganho é zero).
 *
 * Decisão: NÃO vale a complexidade agora. Motivos:
 *  1. `pt` é só um CHUTE (localStorage > navegador) — não dá pra saber em
 *     build-time (o `index.html` é estático, sem personalização por
 *     request) qual idioma o visitante vai realmente resolver. Fixar o
 *     preload em `pt` ajuda quem TEM pt como chute certo e ativamente
 *     DESPERDIÇA bytes de quem tem en/es salvo (baixa um shell que não vai
 *     usar, sem preload nenhum pro shell que precisa de verdade).
 *  2. Implementar de verdade exige um plugin Vite que correlacione, DEPOIS
 *     do build, os módulos do glob de locales (`catalogLoaders` acima) com
 *     os nomes de arquivo final (com hash de conteúdo) dos 4 chunks do
 *     shell `pt` — via `generateBundle`/`transformIndexHtml` — e repetir
 *     isso pros DOIS `index.html` (CE e o overlay EE). Frágil (rolldown é
 *     mais novo, menos precedente da comunidade que Rollup puro) pra um
 *     ganho de 1 RTT só-cache-frio.
 *  3. O ganho estrutural GRANDE desta área já foi resolvido no R3-5.1(c) — a
 *     cascata "esperar `i18n.init()` terminar pra só então descobrir o
 *     idioma e começar a buscar" já não existe mais. O que sobra aqui é bem
 *     menor e de valor variável (depende do chute acertar).
 * Se o boot no cache frio virar um problema medido de verdade (RUM/Core Web
 * Vitals), reavaliar então, com dado real de acerto do chute em produção
 * (não achismo) — o comentário acima já deixa o "porquê não" rastreável.
 */

// Achado real (investigado nesta rodada, via flake intermitente em teste):
// `i18n.init()` faz `this.options = {...defaults, ...this.options, ...novasOpções}`
// — o `...this.options` NO MEIO do spread significa que QUALQUER chave
// deixada por uma chamada de `.init()` ANTERIOR sobre o MESMO singleton
// (`this.options.ns` crescido por `loadNamespaces()`, `this.options.lng`
// gravado por um `changeLanguage()` anterior) SOBREVIVE pra sempre, a menos
// que a chamada NOVA declare a MESMA chave (o que `ns` faz aqui, mas outras
// não). Em produção isto nunca aparece (`.init()` roda uma ÚNICA vez por
// sessão do navegador) — só em teste, onde `vi.resetModules()` reavalia este
// módulo repetidas vezes SEM reiniciar o pacote `i18next` em si (confirmado
// empiricamente: o singleton default do pacote sobrevive a `resetModules`,
// provavelmente por causa do dep-optimizer do Vite). Sem este reset, um
// `this.options.lng` deixado por um teste anterior faz o bootstrap INTERNO
// de `.init()` chamar `changeLanguage(lngAntigo, …)` com um idioma DEFINIDO
// (em vez do `undefined` esperado) — e essa chamada usa `this.options.ns`
// (que também pode estar inflado) pra decidir o que carregar via o backend,
// gerando leituras redundantes de fallback pt pra namespaces que este teste
// nunca pediu, cada uma rejeitada (R4-5.1) e deixando esses namespaces em
// estado -1 ANTES mesmo do 1º `useTranslation` da tela rodar.
i18n.options = {} as typeof i18n.options
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
    // react-i18next) ou pelo prefetch de idle.
    // Array NOVO (spread), não a MESMA referência de `SHELL_NAMESPACES`: o
    // `i18next.loadNamespaces()` faz `this.options.ns.push(n)` — se fosse o
    // mesmo array, cada `useTranslation(ns)` de tela MUTARIA `SHELL_NAMESPACES`
    // in-place, e `isShellNs`/`loadLocale()` (que iteram essa constante em
    // outros pontos deste módulo) passariam a tratar QUALQUER namespace já
    // pedido uma vez como se fosse shell (bug real, pego pelo teste
    // R3-5.1(b) antes de chegar em produção).
    ns: [...SHELL_NAMESPACES],
    defaultNS: "common",
    interpolation: { escapeValue: false },
    returnNull: false,
    // R2-5.1: Suspense por namespace. Em teste, `./testing` já deixou tudo
    // carregado antes do 1º render (ver docstring do topo) — `ready` já
    // nasce `true`, então o `throw` do react-i18next nunca é alcançado lá.
    // R4-5.1(c): `bindI18nStore: "added"` — sem isto (o padrão é `''`, ou
    // seja, NENHUM evento de store), um componente já montado não
    // re-renderiza quando um bundle chega DEPOIS por qualquer caminho que não
    // seja `languageChanged` (prefetch de idle, o pré-carregamento do
    // `changeLanguage` patchado acima, um `useTranslation` concorrente de
    // outro componente) — o texto ficava obsoleto na tela até algum outro
    // motivo forçar um re-render.
    react: { useSuspense: true, bindI18nStore: "added" },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      lookupLocalStorage: LOCALE_STORAGE_KEY,
      caches: ["localStorage"],
    },
  })

/**
 * Resolve quando o SHELL do idioma INICIAL terminou de carregar.
 * `main.tsx` aguarda isto antes do 1º `render()`. Namespaces de tela (fora do
 * shell) carregam depois, sob demanda (Suspense) e por prefetch de idle.
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
  // R3-5.1(a): agenda o prefetch dos namespaces restantes assim que o SHELL
  // (o que bloqueia o 1º render) está pronto — não espera o render de
  // verdade acontecer, só o suficiente pra não competir com ele.
  schedulePrefetch(resolved)
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
