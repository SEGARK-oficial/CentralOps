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
import { describe, it, expect } from "vitest"

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
