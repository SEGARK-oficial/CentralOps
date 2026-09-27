/**
 * tailwindTokenGuard — detector de classes Tailwind de COR que não geram CSS
 * nenhum (nem token do design system, nem cor/utility legítima do Tailwind).
 *
 * Motivação: 81 usos de `text-muted` sobreviveram em produção porque `--color-
 * muted` nunca existiu — Tailwind v4 simplesmente NÃO gera a classe (nenhum
 * erro de build, nenhum warning), e o elemento fica com a cor herdada do pai.
 * Nada no pipeline (build, lint, testes existentes) detectava isso.
 *
 * Compartilhado entre o guard do Core (`src/__tests__/`) e o do overlay EE
 * (`web-ee/test/`, via alias `@/test/tailwindTokenGuard` — o mesmo catch-all
 * `@` que resolve `@/test/setup` no setup do overlay).
 *
 * Escopo deliberado: isto NÃO é um parser completo da gramática do Tailwind.
 * Cobre os prefixos de cor listados abaixo, reconhece os usos NÃO-cor mais
 * comuns que compartilham o mesmo prefixo (tamanho, alinhamento, estilo,
 * largura — ex. `text-xs`, `border-2`, `text-left`), e valida o resto contra:
 * (a) os tokens `--color-*` declarados em `globals.css`, (b) a paleta padrão
 * do Tailwind (family-shade, ex. `red-500`), (c) as palavras-chave de cor do
 * CSS (`transparent`/`current`/`inherit`/`black`/`white`).
 */
import { readFileSync, readdirSync, statSync } from "node:fs"
import { join } from "node:path"

export const COLOR_PREFIXES = [
  "bg",
  "text",
  "border",
  "ring",
  "outline",
  "fill",
  "stroke",
  "from",
  "to",
  "via",
  "divide",
  "placeholder",
  "decoration",
  "shadow",
  "accent",
  "caret",
] as const

const KEYWORD_COLORS = new Set(["transparent", "current", "currentcolor", "inherit", "black", "white"])

const DEFAULT_FAMILIES = new Set([
  "slate", "gray", "grey", "zinc", "neutral", "stone",
  "red", "orange", "amber", "yellow", "lime", "green", "emerald",
  "teal", "cyan", "sky", "blue", "indigo", "violet", "purple",
  "fuchsia", "pink", "rose",
])
const DEFAULT_SHADES = new Set(["50", "100", "200", "300", "400", "500", "600", "700", "800", "900", "950"])

// Sufixos que compartilham o namespace `<prefixo>-` de uma classe de cor mas
// significam outra coisa (tamanho, alinhamento, estilo, direção de gradiente,
// modo de blend, largura de borda/anel/decoração, offset...). Ficam de fora
// da checagem de cor por design — não são "phantom", são outra propriedade.
const NON_COLOR_SUFFIXES = new Set([
  // bg-* (posição/repetição/blend/gradiente/clip/origin)
  "none", "auto", "cover", "contain", "top", "bottom", "left", "right", "center",
  "top-left", "top-right", "bottom-left", "bottom-right",
  "repeat", "repeat-x", "repeat-y", "repeat-round", "repeat-space", "no-repeat",
  "fixed", "local", "scroll",
  "clip-border", "clip-padding", "clip-content", "clip-text",
  "origin-top", "origin-top-right", "origin-right", "origin-bottom-right",
  "origin-bottom", "origin-bottom-left", "origin-left", "origin-top-left", "origin-center",
  "blend-normal", "blend-multiply", "blend-screen", "blend-overlay", "blend-darken",
  "blend-lighten", "blend-color-dodge", "blend-color-burn", "blend-hard-light",
  "blend-soft-light", "blend-difference", "blend-exclusion", "blend-hue",
  "blend-saturation", "blend-color", "blend-luminosity",
  "gradient-to-t", "gradient-to-tr", "gradient-to-r", "gradient-to-br",
  "gradient-to-b", "gradient-to-bl", "gradient-to-l", "gradient-to-tl",
  "linear-to-t", "linear-to-tr", "linear-to-r", "linear-to-br",
  "linear-to-b", "linear-to-bl", "linear-to-l", "linear-to-tl",
  // text-* (alinhamento/tamanho/quebra)
  "justify", "start", "end", "wrap", "nowrap", "balance", "pretty",
  "ellipsis", "clip", "truncate",
  "xs", "sm", "base", "lg", "xl", "2xl", "3xl", "4xl", "5xl", "6xl", "7xl", "8xl", "9xl",
  // border-*/divide-*/outline-*/ring-*/decoration-* (estilo)
  "solid", "dashed", "dotted", "double", "hidden", "collapse", "separate",
  // shadow-* (tamanho — não confundir com token de cor; "md" só existe aqui,
  // não na escala de text-size acima)
  "2xs", "md", "inner",
  // outline/ring/decoration (offset e largura nomeada)
  "offset",
  // border-*/divide-* SEM largura/cor explícita (ex.: `border-b`, `divide-y`
  // sozinhos = largura default 1px daquele lado) — letra direcional bare.
  "t", "r", "b", "l", "x", "y", "s", "e",
  // "color" bare nunca é um nome de token/família — mas É o sufixo de nomes
  // de PROPRIEDADE CSS (não classe) que aparecem dentro de listas arbitrárias
  // como `transition-[box-shadow,border-color,transform]`. Sem isto, o
  // "border-color" daquela lista (propriedade, não classe) seria lido como
  // `border-<color>` e reprovado por não ter cor nenhuma chamada "color".
  "color",
])

// Infixo direcional que aparece ANTES da cor em border-*/divide-*
// (ex.: `border-t-red-500`, `divide-x-primary-400`) — removido antes de
// avaliar o resto como nome de cor.
const DIRECTIONAL_INFIX = /^(t|r|b|l|x|y|s|e)-/

/** Extrai os nomes de token (`--color-<nome>`) declarados num CSS. */
export function extractColorTokens(cssContent: string): Set<string> {
  const tokens = new Set<string>()
  const re = /--color-([a-zA-Z0-9-]+)\s*:/g
  let m: RegExpExecArray | null
  while ((m = re.exec(cssContent))) tokens.add(m[1])
  return tokens
}

function isKnownColorName(name: string, tokens: Set<string>): boolean {
  if (tokens.has(name)) return true
  if (KEYWORD_COLORS.has(name.toLowerCase())) return true
  const parts = name.split("-")
  if (parts.length === 2 && DEFAULT_FAMILIES.has(parts[0]) && DEFAULT_SHADES.has(parts[1])) return true
  return false
}

/**
 * Remove comentários `//` e `/* *\/` de um source TS/TSX sem tocar em
 * conteúdo de string/template (para não confundir uma URL `https://` dentro
 * de uma string com o início de um comentário). Necessário porque este
 * próprio repositório documenta bugs passados citando o nome da classe
 * fantasma no comentário (ex.: "`focus:border-primary` não é um token
 * real") — sem isto, a PROSA vira falso positivo.
 */
export function stripComments(source: string): string {
  let out = ""
  let i = 0
  const n = source.length
  let inLineComment = false
  let inBlockComment = false
  let inString: '"' | "'" | "`" | null = null

  while (i < n) {
    const c = source[i]
    const next = i + 1 < n ? source[i + 1] : ""

    if (inLineComment) {
      if (c === "\n") {
        inLineComment = false
        out += c
      }
      i++
      continue
    }
    if (inBlockComment) {
      if (c === "*" && next === "/") {
        inBlockComment = false
        i += 2
        continue
      }
      i++
      continue
    }
    if (inString) {
      out += c
      if (c === "\\") {
        out += next
        i += 2
        continue
      }
      if (c === inString) inString = null
      i++
      continue
    }
    if (c === "/" && next === "/") {
      inLineComment = true
      i += 2
      continue
    }
    if (c === "/" && next === "*") {
      inBlockComment = true
      i += 2
      continue
    }
    if (c === '"' || c === "'" || c === "`") {
      inString = c
      out += c
      i++
      continue
    }
    out += c
    i++
  }
  return out
}

export interface ScannedClass {
  /** A classe completa como apareceu no source (com variantes/opacidade). */
  raw: string
  prefix: string
  suffix: string
}

// Modificadores (hover:, dark:, sm:, group-hover:, peer-focus:, etc.) + o
// prefixo de cor + o nome + opacidade opcional (/50 ou /[0.5]). A âncora à
// esquerda evita casar dentro de outra palavra (ex.: NÃO casar "context" com
// prefixo "text" — exige borda de palavra/aspas/espaço antes).
function buildClassRegex(): RegExp {
  return new RegExp(
    `(?:^|[\\s"'\`{(,])` +
      `((?:[a-zA-Z0-9_-]+:)*)` +
      `(${COLOR_PREFIXES.join("|")})-` +
      `([a-zA-Z][a-zA-Z0-9-]*)` +
      `(?:/(?:\\d{1,3}|\\[[^\\]]*\\]))?`,
    "g",
  )
}

/** Varre um trecho de source e devolve toda classe candidata a cor. */
export function findColorUtilityClasses(source: string): ScannedClass[] {
  const out: ScannedClass[] = []
  const re = buildClassRegex()
  let m: RegExpExecArray | null
  while ((m = re.exec(source))) {
    const [full, variants, prefix, suffix] = m
    out.push({ raw: (variants + prefix + "-" + suffix).trim(), prefix, suffix })
    void full
  }
  return out
}

export type ClassVerdict = "valid" | "non-color" | "invalid"

/** Classifica uma classe candidata contra os tokens do design system. */
export function classifyClass(cls: ScannedClass, tokens: Set<string>): ClassVerdict {
  let suffix = cls.suffix
  if ((cls.prefix === "border" || cls.prefix === "divide") && DIRECTIONAL_INFIX.test(suffix)) {
    suffix = suffix.replace(DIRECTIONAL_INFIX, "")
  }
  // Sufixo vazio depois de tirar o infixo direcional: era só a letra (ex.:
  // `border-l-[3px]` capturou "l-", o infixo consumiu tudo). Mesmo raciocínio
  // do endsWith("-") abaixo — sem cor nenhuma reivindicada aqui.
  if (suffix === "") return "non-color"
  if (NON_COLOR_SUFFIXES.has(suffix)) return "non-color"
  if (/^\d+$/.test(suffix)) return "non-color" // largura pura (border-2, ring-4, decoration-2...)
  // `outline-offset-2`/`ring-offset-4`/`decoration-offset-...`: offset é uma
  // propriedade de posicionamento, não de cor, mesmo com um número atrás.
  if (suffix.startsWith("offset-") || suffix === "offset") return "non-color"
  // Sufixo cortado ANTES de um valor arbitrário entre colchetes (ex.:
  // `border-l-[3px]` → o regex captura só "l-" antes do "["). Um valor
  // arbitrário do Tailwind SEMPRE gera CSS — não há "fantasma" possível aqui,
  // e nenhum token nosso termina em hífen solto.
  if (suffix.endsWith("-")) return "non-color"
  if (isKnownColorName(suffix, tokens)) return "valid"
  return "invalid"
}

const EXCLUDED_DIR_NAMES = new Set(["node_modules", "__tests__", "test", "tests"])

export interface WalkOptions {
  /** Extensões incluídas (ex.: [".ts", ".tsx"]). */
  extensions: string[]
  /** Sufixos de arquivo a pular (ex.: [".test.ts", ".test.tsx"]). */
  excludeFileSuffixes?: string[]
}

/** Varre um diretório recursivamente por caminho — sem __dirname/import.meta.url;
 *  o chamador decide a raiz (relativa a `process.cwd()`). Pula diretórios de
 *  teste inteiros (__tests__/test/tests/node_modules) e dotfiles. */
export function walkFiles(rootDir: string, opts: WalkOptions): string[] {
  const out: string[] = []
  const stack = [rootDir]
  while (stack.length > 0) {
    const dir = stack.pop() as string
    let entries: string[]
    try {
      entries = readdirSync(dir)
    } catch {
      continue
    }
    for (const entry of entries) {
      if (entry.startsWith(".")) continue
      const full = join(dir, entry)
      let isDir: boolean
      try {
        isDir = statSync(full).isDirectory()
      } catch {
        continue
      }
      if (isDir) {
        if (EXCLUDED_DIR_NAMES.has(entry)) continue
        stack.push(full)
      } else if (
        opts.extensions.some((ext) => entry.endsWith(ext)) &&
        !opts.excludeFileSuffixes?.some((s) => entry.endsWith(s))
      ) {
        out.push(full)
      }
    }
  }
  return out
}

export interface GuardScanResult {
  valid: ScannedClass[]
  invalid: { file: string; cls: ScannedClass }[]
}

/** Roda o guard completo: lê `globals.css`, varre `srcRoot`, classifica. */
export function scanForPhantomColorClasses(globalsCssPath: string, srcRoot: string): GuardScanResult {
  const tokens = extractColorTokens(readFileSync(globalsCssPath, "utf8"))
  const files = walkFiles(srcRoot, {
    extensions: [".ts", ".tsx"],
    excludeFileSuffixes: [".test.ts", ".test.tsx"],
  })

  const valid: ScannedClass[] = []
  const invalid: { file: string; cls: ScannedClass }[] = []

  for (const file of files) {
    const content = stripComments(readFileSync(file, "utf8"))
    for (const cls of findColorUtilityClasses(content)) {
      const verdict = classifyClass(cls, tokens)
      if (verdict === "valid") valid.push(cls)
      else if (verdict === "invalid") invalid.push({ file, cls })
    }
  }

  return { valid, invalid }
}
