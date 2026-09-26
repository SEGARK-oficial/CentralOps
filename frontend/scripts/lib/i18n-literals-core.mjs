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
// R3-9.2: ampliado além do a11y (aria-label/title/placeholder/alt) para as
// props de TEXTO dos primitivos do design system (`label=`, `text=`,
// `message=`, `description=`, `helperText=`, `emptyLabel=`) — o mesmo bug
// (string sem `t()`) só que num componente próprio em vez de um atributo
// HTML nativo.
const ATTR_RE =
  /\b(aria-label|title|placeholder|alt|label|text|message|description|helperText|emptyLabel)=(?:"([^"]*)"|\{\s*["'`]([^"'`]*)["'`]\s*\})/g

// R3-9.2 — ternário/`&&` DENTRO de um bloco `{...}` do JSX com AMBOS os ramos
// (ou o único ramo do `&&`) sendo string literal — o padrão comum de "texto
// condicional esquecido do t()": `{ativo ? "Ativo" : "Inativo"}`,
// `{erro && "Campo obrigatório"}`. Escopo por `[^{}]*` (sem chave aninhada)
// pra não confundir com um `{t(cond ? "ns:a" : "ns:b")}` — ali a CHAVE do
// t() já é validada por outro gate (check-i18n.mjs), não por este.
const TERNARY_RE = /\{[^{}]*?\?\s*"([^"]{2,100})"\s*:\s*"([^"]{0,100})"[^{}]*?\}/g
const LOGICAL_AND_RE = /\{[^{}]*?&&\s*"([^"]{2,100})"[^{}]*?\}/g

// `setError("Falha ao...")` direto, OU o padrão bem mais comum no código real
// — fallback do ramo falso de um ternário: `setError(e instanceof Error ?
// e.message : "Falha ao carregar scopes")`. `[^()]*?` (sem parênteses no
// meio) cobre o caso real sem precisar de um parser de expressão completo —
// `e instanceof Error ? e.message : "..."` não tem parênteses nenhum.
const SET_ERROR_RE =
  /\bset(?:Error|Feedback)\(\s*(?:"([^"]{2,120})"|[^()]*?:\s*"([^"]{2,120})"\s*)\)/g

// `throw new Error("...")` com string literal — nem todo throw é visível ao
// usuário (muitos são invariantes de programador, como o `Tabs.tsx` já
// documentado no baseline), mas o padrão em si é candidato: quem consome via
// `err.message` numa tela de erro herda o texto cru.
const THROW_RE = /\bthrow\s+new\s+Error\(\s*"([^"]{2,160})"\s*\)/g

/**
 * Acha literais PT num texto de source já lido (sem tocar disco) — usado
 * tanto pela varredura real (arquivo por arquivo) quanto pelo meta-teste
 * (string injetada em memória).
 *
 * A ASSINATURA (`signature`) é `arquivo:trecho` — SEM o número da linha.
 * Um baseline por linha quebra a cada edição ACIMA do achado (nenhum
 * literal novo, o gate falha do mesmo jeito) — foi o que aconteceu na
 * Rodada 3: `Tabs.tsx:43` virou `Tabs.tsx:44` só porque alguém acrescentou
 * uma linha antes, e o baseline teve que ser reajustado à mão sem
 * necessidade. `line` continua no retorno só para o texto de erro (aponta
 * o operador pro lugar certo), nunca para dedupe/comparação.
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
    for (const m of line.matchAll(TERNARY_RE)) {
      const [, whenTrue, whenFalse] = m
      if (looksLikePortuguese(whenTrue) || looksLikePortuguese(whenFalse)) {
        found.push({ line: idx + 1, snippet: `? "${whenTrue.slice(0, 40)}" : "${whenFalse.slice(0, 40)}"` })
      }
    }
    for (const m of line.matchAll(LOGICAL_AND_RE)) {
      const text = m[1].trim()
      if (looksLikePortuguese(text)) {
        found.push({ line: idx + 1, snippet: `&& "${text.slice(0, 80)}"` })
      }
    }
    for (const m of line.matchAll(SET_ERROR_RE)) {
      const text = (m[1] ?? m[2] ?? "").trim()
      if (looksLikePortuguese(text)) {
        found.push({ line: idx + 1, snippet: `setError(.., "${text.slice(0, 80)}")` })
      }
    }
    for (const m of line.matchAll(THROW_RE)) {
      const text = m[1].trim()
      if (looksLikePortuguese(text)) {
        found.push({ line: idx + 1, snippet: `throw new Error("${text.slice(0, 80)}")` })
      }
    }
  })

  return found.map((f) => ({
    signature: `${relPath}:${f.snippet}`,
    file: relPath,
    line: f.line,
    snippet: f.snippet,
  }))
}

/**
 * Conta ocorrências por assinatura (`arquivo:trecho`, sem linha) — o mesmo
 * texto repetido 3× no mesmo arquivo vira `count: 3`, não 1 entrada "já
 * vista". É isto que permite pegar uma 4ª ocorrência NOVA do mesmo texto
 * mesmo com a assinatura já presente no baseline (um `Set` simples não
 * pegaria: a chave já "existiria").
 */
export function countBySignature(findings) {
  const counts = new Map()
  for (const f of findings) counts.set(f.signature, (counts.get(f.signature) ?? 0) + 1)
  return counts
}

/**
 * Compara as contagens atuais contra o baseline: uma assinatura só é "nova"
 * quando a contagem ATUAL excede a do baseline (reduzir é sempre livre —
 * contagem atual menor não dispara nada).
 */
export function findOverages(currentCounts, baselineCounts) {
  const overages = []
  for (const [signature, current] of currentCounts) {
    const baseline = baselineCounts.get(signature) ?? 0
    if (current > baseline) overages.push({ signature, current, baseline, extra: current - baseline })
  }
  return overages
}

/** Serializa um Map<string, number> pro formato do baseline (objeto ordenado). */
export function countsToBaselineObject(counts) {
  const obj = {}
  for (const key of [...counts.keys()].sort()) obj[key] = counts.get(key)
  return obj
}

/** Lê um baseline (objeto `{ assinatura: count }`) devolvendo um Map — aceita
 *  também o formato ANTIGO (array de strings `arquivo:linha:trecho`, sem
 *  contagem) por compatibilidade de leitura de um baseline não migrado, mas
 *  `check-i18n-literals.mjs --update-baseline` sempre grava o formato novo. */
export function baselineToCounts(baselineRaw) {
  if (Array.isArray(baselineRaw)) {
    const counts = new Map()
    for (const entry of baselineRaw) counts.set(entry, (counts.get(entry) ?? 0) + 1)
    return counts
  }
  const counts = new Map()
  for (const [key, value] of Object.entries(baselineRaw ?? {})) counts.set(key, value)
  return counts
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
