/**
 * Testes de labels.ts
 *
 * O guarda `npm run i18n:check` só enxerga `t("chave.literal")` no código, e
 * aqui a chave chega por variável. Sem este teste, um rótulo de status podia
 * sair cru na tela em qualquer idioma sem nada reclamar.
 */

import { describe, it, expect } from "vitest"
import { AUTH_STATUS_LABEL_KEY, authStatusLabelKey, authStatusVariant } from "@/lib/labels"
import { resources, SUPPORTED_LOCALES } from "@/i18n"

const lookup = (catalog: unknown, path: string[]): unknown =>
  path.reduce<unknown>((node, part) => (node as Record<string, unknown>)?.[part], catalog)

describe("authStatusLabelKey", () => {
  it("devolve chave i18n, nunca texto pronto", () => {
    expect(authStatusLabelKey("healthy")).toBe("common:states.healthy")
    expect(authStatusLabelKey("error")).toBe("common:states.error")
  })

  it("normaliza caixa", () => {
    expect(authStatusLabelKey("HEALTHY")).toBe(authStatusLabelKey("healthy"))
  })

  it("ausente cai em desconhecido", () => {
    expect(authStatusLabelKey(null)).toBe("common:states.unknown")
    expect(authStatusLabelKey(undefined)).toBe("common:states.unknown")
  })

  it("enum novo do backend passa cru, sem esconder informação", () => {
    expect(authStatusLabelKey("rate_limited")).toBe("rate_limited")
  })

  it("toda chave existe nos três catálogos", () => {
    for (const key of Object.values(AUTH_STATUS_LABEL_KEY)) {
      const [ns, path] = key.split(":")
      for (const locale of SUPPORTED_LOCALES) {
        const value = lookup(resources[locale]?.[ns], path.split("."))
        expect(typeof value, `${locale}/${ns}.json → ${path}`).toBe("string")
      }
    }
  })
})

describe("authStatusVariant", () => {
  // Saudável é repouso, e repouso é neutro. Só o que quebrou sozinho ganha cor.
  it("healthy é neutro", () => {
    expect(authStatusVariant("healthy")).toBe("default")
  })

  it("degradado e erro carregam matiz", () => {
    expect(authStatusVariant("degraded")).toBe("warning")
    expect(authStatusVariant("error")).toBe("danger")
  })

  it("desconhecido e ausente ficam em contorno", () => {
    expect(authStatusVariant("unknown")).toBe("outline")
    expect(authStatusVariant(null)).toBe("outline")
  })
})
