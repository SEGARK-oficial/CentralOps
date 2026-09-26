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
import { createElement, Suspense } from "react"
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

    // Antes do backend resolver: o fallback do Suspense aparece — a chave
    // crua "list.pageTitle" nunca chega a piscar na tela.
    expect(screen.getByText("carregando-fallback")).toBeInTheDocument()
    expect(screen.queryByText("list.pageTitle")).not.toBeInTheDocument()

    // Depois de resolver: o texto real (via o backend local, sem baixar o
    // idioma inteiro) substitui o fallback.
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

    // Logo após `i18nReady`: nenhuma tela pediu nada ainda, e o prefetch de
    // idle (agendado DENTRO do próprio `i18nReady`) ainda não teve chance de
    // rodar — é síncrono até aqui.
    expect(fresh.default.hasResourceBundle("pt", "mappings")).toBe(false)
    expect(fresh.default.hasResourceBundle("pt", "correlation")).toBe(false)

    // jsdom não tem `requestIdleCallback` — o fallback é `setTimeout(fn, 0)`.
    // Uma volta real ao event loop deixa esse timer (e os `import()`
    // dinâmicos que ele dispara) resolver.
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(fresh.default.hasResourceBundle("pt", "mappings")).toBe(true)
    // Efeito colateral aceito de propósito: o prefetch busca TODOS os
    // namespaces do idioma ativo, "correlation" (só-EE) incluído — o texto
    // continua fora do bundle JS (é só um `import()` de JSON), e a
    // alternativa (nunca eliminar o Suspense tardio pra telas fora do
    // shell) é pior.
    expect(fresh.default.hasResourceBundle("pt", "correlation")).toBe(true)
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
