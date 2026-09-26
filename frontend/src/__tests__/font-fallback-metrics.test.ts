/**
 * R3-6.3: fallback com métrica ajustada (`size-adjust`/`ascent-override`/
 * `descent-override`/`line-gap-override`) para as 3 webfonts self-host
 * (R2-6.2), eliminando o reflow (CLS) do FOUT quando o woff2 troca de lugar
 * com o fallback do sistema.
 *
 * Guard por CONTEÚDO — não há runtime de layout aqui (jsdom não calcula
 * métrica real de fonte), então o teste prova PRESENÇA/COERÊNCIA da receita,
 * não o pixel final.
 */
import { readFileSync } from "node:fs"
import { join } from "node:path"

const ROOT = process.cwd()
const GLOBALS_CSS = readFileSync(join(ROOT, "src", "styles", "globals.css"), "utf8")

function extractFontFace(family: string): string {
  const re = new RegExp(
    `@font-face\\s*\\{[^}]*font-family:\\s*"${family}"[^}]*\\}`,
    "s",
  )
  const match = GLOBALS_CSS.match(re)
  if (!match) throw new Error(`@font-face "${family}" não encontrado em globals.css`)
  return match[0]
}

describe("R3-6.3 — @font-face de fallback com métrica ajustada", () => {
  it.each([
    ["Archivo Fallback", ["Arial", "Helvetica"]],
    ["IBM Plex Sans Fallback", ["Arial", "Helvetica"]],
    ["IBM Plex Mono Fallback", ["Courier New", "Menlo"]],
  ])("%s existe, aponta pra fonte local e declara as 4 métricas", (family, locals) => {
    const rule = extractFontFace(family)
    for (const local of locals) {
      expect(rule, `${family} deveria ter local("${local}")`).toMatch(
        new RegExp(`local\\("${local}"\\)`),
      )
    }
    expect(rule).toMatch(/size-adjust:\s*[\d.]+%/)
    expect(rule).toMatch(/ascent-override:\s*[\d.]+%/)
    expect(rule).toMatch(/descent-override:\s*[\d.]+%/)
    expect(rule).toMatch(/line-gap-override:\s*[\d.]+%/)
  })

  it("as 3 famílias de fallback aparecem nas pilhas --font-* (display/sans/mono), logo após a webfont real", () => {
    expect(GLOBALS_CSS).toMatch(
      /--font-display:\s*"Archivo",\s*"Archivo Fallback",[^;]*"IBM Plex Sans",\s*"IBM Plex Sans Fallback"/,
    )
    expect(GLOBALS_CSS).toMatch(/--font-sans:\s*"IBM Plex Sans",\s*"IBM Plex Sans Fallback"/)
    expect(GLOBALS_CSS).toMatch(/--font-mono:\s*"IBM Plex Mono",\s*"IBM Plex Mono Fallback"/)
  })

  it("size-adjust fica numa faixa plausível (80%-160%) — detecta erro grosseiro de cálculo/unidade", () => {
    for (const family of ["Archivo Fallback", "IBM Plex Sans Fallback", "IBM Plex Mono Fallback"]) {
      const rule = extractFontFace(family)
      const sizeAdjust = Number(rule.match(/size-adjust:\s*([\d.]+)%/)?.[1])
      expect(sizeAdjust).toBeGreaterThan(80)
      expect(sizeAdjust).toBeLessThan(160)
    }
  })

  it("meta-caso: um @font-face de fallback SEM as 4 métricas falha a asserção (prova que o teste detecta ausência)", () => {
    const fakeCss = `
      @font-face {
        font-family: "Fake Fallback";
        src: local("Arial");
      }
    `
    const re = /@font-face\s*\{[^}]*font-family:\s*"Fake Fallback"[^}]*\}/s
    const rule = fakeCss.match(re)?.[0] ?? ""
    expect(rule).not.toMatch(/size-adjust:\s*[\d.]+%/)
  })
})
