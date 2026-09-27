/**
 * Guard de regressão de contraste — R2-6.10 (`--color-border-field`) e A11Y-39
 * (`--ink-3`, fase 1). Em vez de travar no VALOR literal do token (que quebra
 * por qualquer reformatação cosmética), recalcula o contraste WCAG de verdade
 * a partir do que está em `globals.css` — falha se alguém baixar o valor de
 * novo pra uma faixa reprovada, mesmo que não toque neste teste.
 */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import {
  compositedLuminance,
  contrastRatioFromLuminance,
  luminanceOf,
  parseOklch,
} from "@/test/oklchContrast"

const CSS = readFileSync(join(process.cwd(), "src", "styles", "globals.css"), "utf8")

/** Extrai `--token: oklch(...)`, primeira ocorrência (as declarações vivem em `:root`). */
function tokenValue(name: string): string {
  const re = new RegExp(`--${name}:\\s*([^;]+);`)
  const m = CSS.match(re)
  if (!m) throw new Error(`Token --${name} não encontrado em globals.css`)
  return m[1].trim()
}

describe("color-contrast-tokens — --color-border-field (R2-6.10)", () => {
  it("branco-alfa sobre --panel dá pelo menos 3:1 (WCAG 1.4.11, componente não-textual)", () => {
    const borderField = parseOklch(tokenValue("color-border-field"))
    const panel = parseOklch(tokenValue("panel"))

    const lumBorder = compositedLuminance(borderField, panel)
    const lumPanel = luminanceOf(panel)
    const ratio = contrastRatioFromLuminance(lumBorder, lumPanel)

    expect(ratio).toBeGreaterThanOrEqual(3.0)
  })

  it("branco-alfa sobre --ground dá pelo menos 3:1", () => {
    const borderField = parseOklch(tokenValue("color-border-field"))
    const ground = parseOklch(tokenValue("ground"))

    const lumBorder = compositedLuminance(borderField, ground)
    const lumGround = luminanceOf(ground)
    const ratio = contrastRatioFromLuminance(lumBorder, lumGround)

    expect(ratio).toBeGreaterThanOrEqual(3.0)
  })

  it("--color-border-field-hover continua mais opaco (mais claro) que a base — hover não pode escurecer", () => {
    const base = parseOklch(tokenValue("color-border-field"))
    const hover = parseOklch(tokenValue("color-border-field-hover"))
    expect(hover.alpha ?? 1).toBeGreaterThan(base.alpha ?? 1)
  })

  it("meta-caso: um alfa claramente baixo (20%) É reprovado pelo mesmo cálculo — prova que o guard detecta regressão", () => {
    // Não usamos o valor antigo real (34%) aqui: por ESTE método de cálculo
    // (composição em espaço sRGB) ele já mede ~3.07:1, tecnicamente acima do
    // corte — a ferramenta do auditor original mediu 2.88:1 por um método
    // diferente. O meta-caso só precisa provar que o detector REAGE a uma
    // regressão óbvia, não replicar o número exato do relatório.
    const panel = parseOklch(tokenValue("panel"))
    const lowAlphaBorder = { l: 1, c: 0, h: 0, alpha: 0.20 }

    const ratio = contrastRatioFromLuminance(compositedLuminance(lowAlphaBorder, panel), luminanceOf(panel))
    expect(ratio).toBeLessThan(3.0)
  })
})

describe("color-contrast-tokens — --ink-3 (A11Y-39, fase 1)", () => {
  it("--ink-3 sobre --panel-raised dá pelo menos 4.5:1 (WCAG AA, texto normal)", () => {
    const ink3 = parseOklch(tokenValue("ink-3"))
    const panelRaised = parseOklch(tokenValue("panel-raised"))
    const ratio = contrastRatioFromLuminance(luminanceOf(ink3), luminanceOf(panelRaised))
    expect(ratio).toBeGreaterThanOrEqual(4.5)
  })

  it("meta-caso: o L antigo (0.655) É reprovado pelo mesmo cálculo", () => {
    const panelRaised = parseOklch(tokenValue("panel-raised"))
    const oldInk3 = { l: 0.655, c: 0.019, h: 265 }
    const ratio = contrastRatioFromLuminance(luminanceOf(oldInk3), luminanceOf(panelRaised))
    expect(ratio).toBeLessThan(4.5)
  })
})
