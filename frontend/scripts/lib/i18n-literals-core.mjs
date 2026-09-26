/**
 * Núcleo (puro, sem I/O) da catraca de literais PT — R2-9.3.
 *
 * Separado de `scripts/check-i18n-literals.mjs` (que faz a varredura de
 * arquivos e a orquestração de CLI) para ser testável por vitest: o meta-teste
 * em `src/__tests__/i18n-literals-guard.test.ts` importa `findingsInText`
 * diretamente e prova, com uma string injetada, que o detector pega — sem
 * depender de rodar o processo Node inteiro.
 */
import { readdirSync, statSync } from "node:fs"
import { join } from "node:path"

// ── Heurística de "isto parece PT" ────────────────────────────────────────────
const HAS_ACCENT = /[À-ÿ]/ // cobre á,ã,ç,é,ê,í,ó,ô,õ,ú,ü e maiúsculas
// Palavras funcionais de PT comuns em UI que NÃO têm acento — cada uma checada
// como palavra inteira (\b), case-insensitive.
export const PT_WORDS = new RegExp(
  "\\b(" +
    [
      "de", "da", "do", "das", "dos", "para", "com", "sem", "ou", "mais",
      "novo", "nova", "salvar", "cancelar", "confirmar", "sim", "erro",
      "sucesso", "falha", "carregando", "selecionar", "adicionar", "remover",
      "excluir", "buscar", "nenhum", "nenhuma", "todos", "todas", "senha",
      "campo", "obrigatorio", "invalido", "atualizar", "voltar", "proximo",
      "anterior", "fechar", "abrir", "editar", "criar", "enviar",
    ].join("|") +
    ")\\b",
  "i",
)

export function looksLikePortuguese(text) {
  const t = text.trim()
  if (t.length < 2) return false
  if (!/[a-zA-ZÀ-ÿ]/.test(t)) return false // sem nenhuma letra — número/símbolo
  if (HAS_ACCENT.test(t)) return true
  return PT_WORDS.test(t)
}

// Remove comentários `//` e `/* */` (inclusive `{/* jsx */}`) preservando as
// quebras de linha (pra não desalinhar o número de linha reportado). Sem
// isto, um comentário que MENCIONA tags JSX na prosa (ex.: "`<p>` em vez de
// `<h1>`: ...") engana o regex de texto — o próprio ">" e "<" da prosa viram
// "abertura e fechamento de tag" pro heurístico. Não toca em conteúdo de
// string (aspas/backtick), pela mesma razão do guard de tokens Tailwind
// (`src/test/tailwindTokenGuard.ts`).
export function stripComments(source) {
  let out = ""
  let i = 0
  const n = source.length
  let inLineComment = false
  let inBlockComment = false
  let inString = null

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
      if (c === "\n") out += c
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

// Texto puro entre duas tags JSX, sem NENHUM `{`/`}` no meio — o instante em
// que aparece uma chave `{`, é uma expressão JS (`{t("...")}`, `{variavel}`,
// `{cond ? a : b}`), fora do escopo deste heurístico (texto MISTO com
// expressão no meio não é pego — sub-detecção deliberada, não falso-negativo
// perigoso: o texto que SOBRA como filho puro de um elemento é o caso comum
// de um label esquecido).
const JSX_TEXT_RE = />([^<>{}]{2,})</g

// Atributos de acessibilidade/UX que carregam texto pro usuário — estático
// (`aria-label="..."`) ou em chave JS (`aria-label={"..."}`/`{'...'}`).
const ATTR_RE =
  /\b(aria-label|title|placeholder|alt)=(?:"([^"]*)"|\{\s*["'`]([^"'`]*)["'`]\s*\})/g

/**
 * Acha literais PT num texto de source já lido (sem tocar disco) — usado
 * tanto pela varredura real (arquivo por arquivo) quanto pelo meta-teste
 * (string injetada em memória).
 */
export function findingsInText(rawSource, relPath) {
  const content = stripComments(rawSource)
  const lines = content.split("\n")
  const found = []

  lines.forEach((line, idx) => {
    for (const m of line.matchAll(JSX_TEXT_RE)) {
      const text = m[1].trim()
      if (looksLikePortuguese(text)) {
        found.push({ line: idx + 1, snippet: text.slice(0, 80) })
      }
    }
    for (const m of line.matchAll(ATTR_RE)) {
      const value = (m[2] ?? m[3] ?? "").trim()
      if (looksLikePortuguese(value)) {
        found.push({ line: idx + 1, snippet: `${m[1]}="${value.slice(0, 70)}"` })
      }
    }
  })

  return found.map((f) => ({
    signature: `${relPath}:${f.line}:${f.snippet}`,
    file: relPath,
    line: f.line,
    snippet: f.snippet,
  }))
}

const EXCLUDED_DIR_NAMES = new Set(["node_modules", "i18n", "__tests__", "test", "tests"])

/** Varre um diretório recursivamente por caminho (sem __dirname/import.meta.url
 *  no chamador — quem decide a raiz é ele). Pula testes/catálogos/dotfiles. */
export function* walkTsFiles(dir) {
  for (const name of readdirSync(dir)) {
    if (EXCLUDED_DIR_NAMES.has(name) || name.startsWith(".")) continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) {
      yield* walkTsFiles(p)
    } else if (/\.tsx?$/.test(name) && !/\.test\./.test(name)) {
      yield p
    }
  }
}
