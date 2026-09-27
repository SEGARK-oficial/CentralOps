/**
 * PERF-01 — carga preguiçosa por idioma.
 *
 * `src/test/setup.ts` importa `@/i18n/testing` (glob EAGER) para todo o resto
 * da suíte poder trocar de idioma e ler `resources` sem `await`. Isso deixa
 * os 3 catálogos residentes ANTES de qualquer teste rodar — o que é o
 * comportamento certo para testes, mas some com o sinal que este arquivo
 * precisa observar (idioma ainda NÃO carregado).
 *
 * `vi.resetModules()` + um `import()` dinâmico dão um `i18next` genuinamente
 * fresco, sem a pré-carga do `testing.ts` — o único jeito de exercitar o
 * caminho preguiçoso de verdade dentro do harness de teste.
 */
import { createElement, Suspense, useEffect } from "react"
import { describe, it, expect, beforeEach } from "vitest"
import { render, screen } from "@testing-library/react"
import { I18nextProvider, useTranslation } from "react-i18next"
import { LOCALE_STORAGE_KEY } from "@/i18n"

// O `changeLanguage("es")` de um teste grava em `localStorage` (é assim que o
// idioma escolhido sobrevive a um reload de verdade) — sem limpar entre
// testes, o `LanguageDetector` do PRÓXIMO `import()` fresco lê "es" de volta
// e resolve o idioma errado, já que jsdom não isola localStorage por teste.
beforeEach(() => {
  localStorage.clear()
})

describe("i18n — changeLanguage carrega o catálogo ANTES de resolver (PERF-01)", () => {
  it("i18nReady carrega só o idioma inicial (pt); es continua não-carregado", async () => {
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady

    // jsdom resolve navigator.language como "en-US" por padrão, mas sem
    // nada salvo em localStorage e sem detector configurado nesse instante,
    // o fallback "pt" prevalece — mesmo comportamento documentado em
    // src/i18n/index.ts.
    expect(fresh.default.hasResourceBundle("pt", "common")).toBe(true)
    expect(fresh.default.hasResourceBundle("es", "common")).toBe(false)
  })

  it("changeLanguage('es') só resolve DEPOIS do catálogo carregado — nunca uma chave crua", async () => {
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady
    expect(fresh.default.hasResourceBundle("es", "common")).toBe(false)

    await fresh.default.changeLanguage("es")

    // Se a troca tivesse acontecido ANTES da carga (o bug que este wrapper
    // evita), t() aqui devolveria a chave crua "actions.save" em vez do
    // texto em espanhol.
    expect(fresh.default.hasResourceBundle("es", "common")).toBe(true)
    expect(fresh.default.t("common:actions.save")).not.toBe("actions.save")
    expect(fresh.default.language).toBe("es")
  })

  it("carregar o mesmo idioma duas vezes é idempotente (sem 2º fetch)", async () => {
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady

    await fresh.loadLocale("es")
    const afterFirst = fresh.resources.es?.common
    await fresh.loadLocale("es")
    // Mesmo objeto de catálogo — a 2ª chamada não refez o `import()`.
    expect(fresh.resources.es?.common).toBe(afterFirst)
  })
})

describe("i18n — namespace fora do shell carrega sob demanda com Suspense (R2-5.1)", () => {
  it("suspende até o backend resolver o namespace; nunca mostra chave crua", async () => {
    // jsdom resolve navigator.language como "en-US" por padrão — sem isto, o
    // idioma ativo seria "en" (o pt só entraria como fallback em paralelo,
    // não como idioma corrente), e a asserção de texto abaixo ficaria
    // dependente do ambiente em vez do comportamento testado.
    localStorage.setItem(LOCALE_STORAGE_KEY, "pt")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady

    // "detections" não é SHELL — não deve estar carregado ainda no boot.
    expect(fresh.default.hasResourceBundle("pt", "detections")).toBe(false)

    function Probe() {
      const { t } = useTranslation("detections")
      return createElement("p", null, t("list.pageTitle"))
    }

    render(
      createElement(
        I18nextProvider,
        { i18n: fresh.default },
        createElement(
          Suspense,
          { fallback: createElement("p", null, "carregando-fallback") },
          createElement(Probe, null),
        ),
      ),
    )

    // O que importa (o contrato real, não um detalhe de timing): a chave
    // crua "list.pageTitle" NUNCA chega a piscar na tela, nem antes nem
    // depois do backend resolver. Não afirmamos que "carregando-fallback"
    // necessariamente aparece PRIMEIRO — o `import()` dinâmico do JSON local
    // (sem rede de verdade) pode resolver rápido demais pra observar o
    // fallback de forma determinística entre runs (module cache do Vite
    // aquecido por testes anteriores no mesmo worker); o que não pode
    // acontecer JAMAIS é a chave crua na tela.
    expect(screen.queryByText("list.pageTitle")).not.toBeInTheDocument()

    // Depois de resolver: o texto real (via o backend local, sem baixar o
    // idioma inteiro) aparece — seja substituindo o fallback, seja direto.
    expect(await screen.findByText("Detecções")).toBeInTheDocument()
    expect(fresh.default.hasResourceBundle("pt", "detections")).toBe(true)
    // R3-5.1(a) mudou este contrato de propósito: "mappings" pode chegar
    // pelo prefetch de idle a qualquer momento depois do boot (não só por
    // pedido explícito) — é exatamente o que faz o Suspense tardio (regressão
    // da R2-5.1) virar um caso raro. Ver describe "prefetch de idle" abaixo
    // para o teste que cobre ESSE comportamento.
  })
})

describe("i18n — prefetch de idle carrega o resto do idioma após o boot (R3-5.1a)", () => {
  it("logo após o boot, só o SHELL está carregado; depois do idle, o resto também", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "pt")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady

    // Não afirmamos aqui que "nada carregou ainda logo após `i18nReady`" — o
    // prefetch de idle usa `setTimeout(fn, 0)` (jsdom não tem
    // `requestIdleCallback`), e o `import()` dinâmico por trás do carregador
    // de módulos do Vite pode intercalar voltas de macrotask de forma não
    // determinística entre workers/execuções (achado real: essa asserção
    // "antes" flakava sozinha, sem relação com o comportamento do app — a
    // app nunca prometeu "o prefetch não começou ainda", só que os
    // namespaces restantes chegam. O que segue abaixo é o contrato de
    // verdade).

    // jsdom não tem `requestIdleCallback` — o fallback é `setTimeout(fn, 0)`.
    // Uma volta real ao event loop deixa esse timer (e os `import()`
    // dinâmicos que ele dispara) resolver.
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(fresh.default.hasResourceBundle("pt", "mappings")).toBe(true)
    // R4-5.2 mudou este contrato de propósito: por padrão (ninguém chamou
    // `allowPrefetchNamespaces`, que é o estado CE — ver describe "R4-5.2"
    // abaixo), "correlation" (namespace só-EE, nenhum módulo de `src/` o usa)
    // fica de fora do prefetch de idle, pra não desperdiçar a busca numa
    // instância CE.
    expect(fresh.default.hasResourceBundle("pt", "correlation")).toBe(false)
  })
})

describe("i18n — R4-5.2: prefetch de idle não baixa namespace só-EE por padrão (CE); libera via allowPrefetchNamespaces (EE)", () => {
  it("por padrão (CE): prefetch pula 'correlation', mas continua cobrindo um namespace comum ao Core", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "pt")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady

    await new Promise((resolve) => setTimeout(resolve, 50))

    // Positivo: o prefetch de fato rodou e cobriu um namespace comum.
    expect(fresh.default.hasResourceBundle("pt", "mappings")).toBe(true)
    // Negativo: mas nunca buscou o namespace só-EE.
    expect(fresh.default.hasResourceBundle("pt", "correlation")).toBe(false)
    expect(fresh.resources.pt?.correlation).toBeUndefined()
  })

  // R4-5.2 (correção pós-regressão): `src/i18n/index.ts` NUNCA importa
  // `@/ee/*` — é o overlay EE (`web-ee/routes.tsx`) que, no nível do módulo,
  // chama `allowPrefetchNamespaces(["correlation"])` pra se anunciar. Este
  // teste simula exatamente essa chamada, sem importar nada de `@/ee/*` (o
  // i18n não sabe, nem precisa saber, quem chamou).
  it("depois de allowPrefetchNamespaces(['correlation']): o prefetch passa a cobrir o namespace liberado", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "pt")
    vi.resetModules()
    const fresh = await import("@/i18n")
    fresh.allowPrefetchNamespaces(["correlation"])
    await fresh.i18nReady

    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(fresh.default.hasResourceBundle("pt", "mappings")).toBe(true)
    expect(fresh.default.hasResourceBundle("pt", "correlation")).toBe(true)
    expect(fresh.resources.pt?.correlation).toBeDefined()
  })
})

describe("i18n — R3-5.1(b): sem download duplo do fallback pt para namespace de tela", () => {
  it("com o idioma ativo = en, o namespace de tela carrega pro en mas NÃO baixa o pt de segurança", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "en")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady
    expect(fresh.default.language).toBe("en")

    function Probe() {
      const { t } = useTranslation("detections")
      return createElement("p", null, t("list.pageTitle"))
    }
    render(
      createElement(
        I18nextProvider,
        { i18n: fresh.default },
        createElement(Suspense, { fallback: createElement("p", null, "loading") }, createElement(Probe, null)),
      ),
    )

    expect(await screen.findByText("Detections")).toBeInTheDocument()
    // O namespace de TELA carregou pro idioma ATIVO...
    expect(fresh.default.hasResourceBundle("en", "detections")).toBe(true)
    // ...mas o "pt de segurança" pro MESMO namespace nunca foi BUSCADO — o
    // check-i18n (CI) garante que en/detections já tem toda chave que
    // pt/detections tem, então esse fallback nunca cobriria nada de verdade.
    // (Não usamos `hasResourceBundle` aqui: o próprio i18next marca a chamada
    // "satisfeita" com `{}` pra não tentar de novo — o que importa é que
    // NENHUM dado de verdade foi baixado/injetado por `ensureNamespaceLoaded`.)
    expect(fresh.resources.pt?.detections).toBeUndefined()
    // O SHELL continua com o fallback pt de verdade (é o próprio idioma de
    // segurança do boot, já em memória de qualquer forma).
    expect(fresh.default.hasResourceBundle("pt", "common")).toBe(true)
  })
})

describe("i18n — R4-5.1: trocar para pt depois de usar um namespace de tela nunca mostra chave crua", () => {
  // Cadeia do bug (ver docstring de src/i18n/index.ts): usar um namespace de
  // tela com en/es ativo dispara, POR BAIXO DOS PANOS, um passe de fallback
  // pt|ns "redundante" (a R3-5.1(b) já filtra esse passe pra não custar uma
  // 2ª rede). Antes desta rodada, esse passe filtrado ainda gravava um bundle
  // VAZIO marcado como "carregado" — e trocar de verdade pra pt depois
  // encontrava esse bundle vazio e nunca buscava o real. Reverter QUALQUER
  // parte do fix (o `callback(err, false)` em vez de `callback(null, {})`,
  // OU o pré-carregamento no `changeLanguage`) faz este teste falhar de
  // volta com a chave crua "list.pageTitle" na tela.
  it("en -> pt: usa 'detections' em en (dispara o passe redundante) e depois troca pra pt", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "en")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady
    expect(fresh.default.language).toBe("en")

    function Probe() {
      const { t } = useTranslation("detections")
      return createElement("p", null, t("list.pageTitle"))
    }
    render(
      createElement(
        I18nextProvider,
        { i18n: fresh.default },
        createElement(Suspense, { fallback: createElement("p", null, "loading") }, createElement(Probe, null)),
      ),
    )
    // Estabelece o "usado em en" que aciona o passe redundante pt|detections
    // por baixo dos panos (ver R3-5.1(b) acima).
    expect(await screen.findByText("Detections")).toBeInTheDocument()

    await fresh.default.changeLanguage("pt")

    expect(await screen.findByText("Detecções")).toBeInTheDocument()
    expect(screen.queryByText("list.pageTitle")).not.toBeInTheDocument()
  })

  it("es -> pt: caso inverso (outro idioma não-pt) — mesmo mecanismo, mesma garantia", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "es")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady
    expect(fresh.default.language).toBe("es")

    function Probe() {
      const { t } = useTranslation("detections")
      return createElement("p", null, t("list.pageTitle"))
    }
    render(
      createElement(
        I18nextProvider,
        { i18n: fresh.default },
        createElement(Suspense, { fallback: createElement("p", null, "loading") }, createElement(Probe, null)),
      ),
    )
    expect(await screen.findByText("Detecciones")).toBeInTheDocument()

    await fresh.default.changeLanguage("pt")

    expect(await screen.findByText("Detecções")).toBeInTheDocument()
    expect(screen.queryByText("list.pageTitle")).not.toBeInTheDocument()
  })
})

describe("i18n — catálogo que chega DEPOIS do mount não re-dispara loaders com `t` nas deps", () => {
  // Regressão da rodada 4: `react.bindI18nStore: "added"` trocava a identidade
  // de `t` a cada `addResourceBundle` (prefetch de idle ≈ 15 namespaces por
  // boot). Os ~44 loaders do app com `t` nas deps (PlatformContext,
  // EditionContext, páginas de detalhe…) refaziam fetch e remontavam a tela
  // em rajada, desmontando formulário aberto.
  it("adicionar bundles após o mount executa o efeito do loader UMA vez só", async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, "pt")
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady

    let runs = 0
    function Loader() {
      const { t } = useTranslation("common")
      useEffect(() => {
        runs++
      }, [t])
      return createElement("p", null, t("actions.save"))
    }

    render(createElement(I18nextProvider, { i18n: fresh.default }, createElement(Loader, null)))
    expect(await screen.findByText("Salvar")).toBeInTheDocument()
    const afterMount = runs
    // Positivo: o efeito rodou no mount (senão o "não re-executa" seria vácuo).
    expect(afterMount).toBeGreaterThanOrEqual(1)

    // Simula catálogos chegando depois (prefetch de idle / carga sob demanda).
    for (const ns of ["x1", "x2", "x3", "x4", "x5"]) {
      fresh.default.addResourceBundle("pt", ns, { k: "v" }, true, true)
    }
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(runs).toBe(afterMount)
  })
})
