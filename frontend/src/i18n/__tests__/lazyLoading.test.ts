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
import { render, screen, waitFor } from "@testing-library/react"
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
    // Só o namespace pedido carregou — não o idioma inteiro.
    expect(fresh.default.hasResourceBundle("pt", "mappings")).toBe(false)
  })

  it("um namespace só-EE (correlation) nunca carrega no CE sem alguém pedir", async () => {
    vi.resetModules()
    const fresh = await import("@/i18n")
    await fresh.i18nReady
    await waitFor(() => expect(fresh.default.hasResourceBundle("pt", "common")).toBe(true))

    // Nada no CE chamou useTranslation("correlation") — continua ausente.
    expect(fresh.default.hasResourceBundle("pt", "correlation")).toBe(false)
  })
})
