/**
 * R2-5.2 — `main.tsx` precisa lidar com `i18nReady` rejeitando (chunk do
 * catálogo caiu antes do 1º render). Sem `.catch()`, a tela ficava em branco
 * pra sempre. Mocka `@/i18n` para forçar a rejeição e confirma que
 * `BootFailureScreen` aparece, com um jeito de recarregar.
 */
import { describe, it, expect, vi, beforeEach } from "vitest"
import { waitFor } from "@testing-library/react"

describe("main.tsx — i18nReady rejeitando não deixa a tela em branco (R2-5.2)", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>'
  })

  it("renderiza o BootFailureScreen quando i18nReady rejeita", async () => {
    vi.resetModules()
    vi.doMock("@/i18n", () => {
      const rejected = Promise.reject(new Error("chunk do catálogo falhou"))
      // Marca como "tratada" pro Node não acusar unhandled rejection — o
      // `.catch()` de verdade (o que este teste cobre) mora em `main.tsx`,
      // encadeado à MESMA promise por fora deste mock.
      rejected.catch(() => {})
      return { i18nReady: rejected }
    })

    // Silencia o console.error esperado (o boot loga o motivo da falha).
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {})

    await import("@/main")

    await waitFor(() => {
      expect(document.body.textContent).toContain("Could not load the interface language")
    })
    const reloadButton = document.querySelector("button")
    expect(reloadButton).not.toBeNull()
    expect(reloadButton?.textContent).toMatch(/Recarregar/)

    errorSpy.mockRestore()
    vi.doUnmock("@/i18n")
  })
})
