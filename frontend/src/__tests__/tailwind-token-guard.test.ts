/**
 * Guard: nenhuma classe Tailwind de COR no Core aponta pra um token fantasma.
 *
 * `text-muted` sobreviveu a 81 usos em produção porque `--color-muted` nunca
 * existiu — Tailwind v4 não gera erro nem warning para uma classe sem token
 * correspondente, só NÃO gera CSS nenhum (o elemento herda a cor do pai, em
 * silêncio). Nenhum lint, build ou teste existente pegava isso.
 *
 * Lê os tokens de `src/styles/globals.css` (fonte da verdade dos `--color-*`)
 * e varre `src/**\/*.{ts,tsx}` (produção, sem testes) por classes usando os
 * prefixos de cor do Tailwind. Ver `src/test/tailwindTokenGuard.ts` para a
 * lógica de extração/classificação (compartilhada com o guard do overlay EE
 * em `web-ee/test/tailwind-token-guard.test.ts`).
 *
 * Sem `__file__`/getsource: os caminhos são relativos a `process.cwd()` (o
 * frontend do Core), não derivados da localização deste arquivo.
 */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import {
  classifyClass,
  extractColorTokens,
  findColorUtilityClasses,
  scanForPhantomColorClasses,
} from "@/test/tailwindTokenGuard"

const GLOBALS_CSS = join(process.cwd(), "src", "styles", "globals.css")
const SRC_ROOT = join(process.cwd(), "src")

describe("guard de tokens Tailwind fantasmas (Core)", () => {
  it("globals.css declara pelo menos um token --color-*", () => {
    const tokens = extractColorTokens(readFileSync(GLOBALS_CSS, "utf8"))
    expect(tokens.size).toBeGreaterThan(0)
  })

  it("meta-caso: o detector PEGA um token fantasma injetado (text-muted)", () => {
    // Prova que o detector funciona ANTES de confiar no resultado negativo
    // abaixo — um guard que nunca falha não prova nada (ver
    // negative-assert-passes-on-empty.md).
    const tokens = extractColorTokens(readFileSync(GLOBALS_CSS, "utf8"))
    expect(tokens.has("muted")).toBe(false)

    const injected = `<div className="flex text-muted items-center">`
    const classes = findColorUtilityClasses(injected)
    const muted = classes.find((c) => c.prefix === "text" && c.suffix === "muted")
    expect(muted).toBeDefined()
    expect(classifyClass(muted!, tokens)).toBe("invalid")
  })

  it("meta-caso: utilities não-cor com o mesmo prefixo NÃO disparam falso positivo", () => {
    const tokens = extractColorTokens(readFileSync(GLOBALS_CSS, "utf8"))
    const injected = `<div className="text-xs border-2 text-left bg-transparent text-inherit ring-offset-4 shadow-sm border-t-2">`
    const classes = findColorUtilityClasses(injected)
    expect(classes.length).toBeGreaterThan(0)
    for (const cls of classes) {
      expect(classifyClass(cls, tokens)).not.toBe("invalid")
    }
  })

  it("todo o código de produção do Core: zero classes de cor sem token/utility legítima", () => {
    const { valid, invalid } = scanForPhantomColorClasses(GLOBALS_CSS, SRC_ROOT)

    // Positivo ao lado do negativo: o scan tem que ter de fato ENCONTRADO
    // classes válidas (senão "zero inválidas" seria vácuo — a fonte secou).
    expect(valid.length).toBeGreaterThan(0)

    if (invalid.length > 0) {
      const preview = invalid
        .slice(0, 20)
        .map(({ file, cls }) => `${file.replace(process.cwd() + "/", "")}: ${cls.raw}`)
        .join("\n")
      throw new Error(
        `${invalid.length} classe(s) de cor sem token nem utility Tailwind legítima:\n${preview}`,
      )
    }
  })
})
