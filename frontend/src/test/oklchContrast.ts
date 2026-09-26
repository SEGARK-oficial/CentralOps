/**
 * oklchContrast — matemática de contraste WCAG 2.x pra tokens OKLCH do
 * design system, usada pelos guards de regressão de contraste (não é código
 * de produção — vive em `src/test/`, ao lado de `tailwindTokenGuard.ts`).
 *
 * Conversão OKLCH → sRGB linear via a matriz pública do próprio autor do
 * OKLab (Björn Ottosson) — a mesma usada pelo browser/CSS Color 4.
 */

export interface Oklch {
  l: number
  c: number
  h: number
  /** 0–1. Ausente = opaco. */
  alpha?: number
}

/** Casa `oklch(L C H)` e `oklch(L C H / AA%)`, com L/C em número puro (não %). */
const OKLCH_RE = /oklch\(\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*(?:\/\s*([\d.]+)%\s*)?\)/i

export function parseOklch(raw: string): Oklch {
  const m = raw.match(OKLCH_RE)
  if (!m) throw new Error(`Não é um oklch(...) reconhecível: "${raw}"`)
  const [, l, c, h, alphaPct] = m
  return {
    l: Number(l),
    c: Number(c),
    h: Number(h),
    alpha: alphaPct !== undefined ? Number(alphaPct) / 100 : undefined,
  }
}

function oklchToLinearSrgb(l: number, c: number, hDeg: number): [number, number, number] {
  const h = (hDeg * Math.PI) / 180
  const a = c * Math.cos(h)
  const b = c * Math.sin(h)

  const l_ = l + 0.3963377774 * a + 0.2158037573 * b
  const m_ = l - 0.1055613458 * a - 0.0638541728 * b
  const s_ = l - 0.0894841775 * a - 1.2914855480 * b

  const ll = l_ ** 3
  const mm = m_ ** 3
  const ss = s_ ** 3

  const r = 4.0767416621 * ll - 3.3077115913 * mm + 0.2309699292 * ss
  const g = -1.2684380046 * ll + 2.6097574011 * mm - 0.3413193965 * ss
  const bb = -0.0041960863 * ll - 0.7034186147 * mm + 1.7076147010 * ss
  return [r, g, bb]
}

function linearToSrgbChannel(c: number): number {
  const v = Math.max(0, Math.min(1, c))
  return v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055
}

function srgbToLinearChannel(c: number): number {
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

function relativeLuminance([r, g, b]: [number, number, number]): number {
  const R = Math.max(0, r)
  const G = Math.max(0, g)
  const B = Math.max(0, b)
  return 0.2126 * R + 0.7152 * G + 0.0722 * B
}

/** Contraste WCAG entre duas cores OKLCH OPACAS (sem alpha). */
export function contrastRatio(a: Oklch, b: Oklch): number {
  const lumA = relativeLuminance(oklchToLinearSrgb(a.l, a.c, a.h))
  const lumB = relativeLuminance(oklchToLinearSrgb(b.l, b.c, b.h))
  const lighter = Math.max(lumA, lumB)
  const darker = Math.min(lumA, lumB)
  return (lighter + 0.05) / (darker + 0.05)
}

/**
 * Compõe `fg` (com alpha) sobre `bg` OPACO — blend feito em espaço sRGB
 * (gama), como navegadores compõem cores sRGB com transparência — e devolve
 * a LUMINÂNCIA RELATIVA (0–1, WCAG) da cor opaca resultante.
 */
export function compositedLuminance(fg: Oklch, bg: Oklch): number {
  const alpha = fg.alpha ?? 1
  const fgSrgb = oklchToLinearSrgb(fg.l, fg.c, fg.h).map(linearToSrgbChannel)
  const bgSrgb = oklchToLinearSrgb(bg.l, bg.c, bg.h).map(linearToSrgbChannel)
  const compSrgb = fgSrgb.map((f, i) => alpha * f + (1 - alpha) * bgSrgb[i])
  const compLin = compSrgb.map(srgbToLinearChannel) as [number, number, number]
  return relativeLuminance(compLin)
}

/** Contraste entre duas luminâncias relativas já calculadas (0–1). */
export function contrastRatioFromLuminance(lumA: number, lumB: number): number {
  const lighter = Math.max(lumA, lumB)
  const darker = Math.min(lumA, lumB)
  return (lighter + 0.05) / (darker + 0.05)
}

export function luminanceOf(color: Oklch): number {
  return relativeLuminance(oklchToLinearSrgb(color.l, color.c, color.h))
}
