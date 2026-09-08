/**
 * Leitura de CSV para tabelas de enriquecimento.
 *
 * Existe porque CMDB, plano de endereçamento e allowlist nascem em planilha, e
 * a única entrada aceita era JSON `{chave: {campo: valor}}` colado numa
 * textarea. A conversão acontecia fora do produto, à mão, e o erro só aparecia
 * como "N linhas inválidas descartadas" — sem dizer QUAIS.
 *
 * Parser próprio em vez de biblioteca: o formato que precisamos ler é RFC 4180
 * simples, e a alternativa custaria uma dependência nova no bundle do console
 * para 60 linhas de código. O que ele cobre e o que não cobre está nos testes.
 */

/** Separadores candidatos, na ordem em que são testados. */
const CANDIDATE_DELIMITERS = [",", ";", "\t", "|"] as const

export type Delimiter = (typeof CANDIDATE_DELIMITERS)[number]

export interface ParsedCsv {
  delimiter: Delimiter
  headers: string[]
  /** Uma entrada por linha de dados, já alinhada aos cabeçalhos. */
  rows: Array<Record<string, string>>
}

/**
 * Descobre o separador pela CONSISTÊNCIA da contagem de colunas, não pela
 * frequência.
 *
 * Contar ocorrências elege a vírgula em qualquer arquivo `pt-BR` com decimais
 * ("10,5") ou endereços ("Rua X, 100"), mesmo quando o separador real é o
 * ponto e vírgula. O que distingue o separador de verdade é produzir o MESMO
 * número de colunas em todas as linhas.
 */
export function detectDelimiter(text: string): Delimiter {
  const lines = text.split(/\r?\n/).filter((l) => l.trim() !== "").slice(0, 20)
  if (lines.length === 0) return ","

  let best: Delimiter = ","
  let bestScore = -1
  for (const d of CANDIDATE_DELIMITERS) {
    const counts = lines.map((l) => splitLine(l, d).length)
    const columns = counts[0]
    if (columns < 2) continue
    const consistent = counts.every((c) => c === columns)
    // Consistência primeiro; entre os consistentes, o que produz mais colunas.
    const score = (consistent ? 1000 : 0) + columns
    if (score > bestScore) {
      bestScore = score
      best = d
    }
  }
  return best
}

/** Divide UMA linha respeitando aspas duplas e o escape `""` do RFC 4180. */
export function splitLine(line: string, delimiter: string): string[] {
  const out: string[] = []
  let field = ""
  let inQuotes = false
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i]
    if (inQuotes) {
      if (ch === '"') {
        if (line[i + 1] === '"') {
          field += '"'
          i += 1
        } else {
          inQuotes = false
        }
      } else {
        field += ch
      }
      continue
    }
    if (ch === '"') {
      inQuotes = true
      continue
    }
    if (ch === delimiter) {
      out.push(field)
      field = ""
      continue
    }
    field += ch
  }
  out.push(field)
  return out.map((f) => f.trim())
}

export function parseCsv(text: string, delimiter?: Delimiter): ParsedCsv {
  // BOM do Excel vira parte do primeiro cabeçalho e faz a coluna-chave sumir da
  // lista de opções — um dos jeitos mais comuns de este fluxo falhar sem erro.
  const clean = text.replace(/^﻿/, "")
  const d = delimiter ?? detectDelimiter(clean)
  const lines = clean.split(/\r?\n/).filter((l) => l.trim() !== "")
  if (lines.length === 0) return { delimiter: d, headers: [], rows: [] }

  const headers = splitLine(lines[0], d).map((h) => h.replace(/^"|"$/g, ""))
  const rows: Array<Record<string, string>> = []
  for (const line of lines.slice(1)) {
    const cells = splitLine(line, d)
    const row: Record<string, string> = {}
    headers.forEach((h, i) => {
      row[h] = cells[i] ?? ""
    })
    rows.push(row)
  }
  return { delimiter: d, headers, rows }
}

// ── validação de chave ──────────────────────────────────────────────────────

/** IPv4/IPv6 com prefixo opcional. Recusa o que o backend recusaria. */
export function isValidCidrOrIp(value: string): boolean {
  const v = value.trim()
  if (!v) return false
  const [addr, prefix, ...rest] = v.split("/")
  if (rest.length > 0) return false

  const isV4 = /^\d{1,3}(\.\d{1,3}){3}$/.test(addr)
  const isV6 = addr.includes(":") && /^[0-9a-fA-F:]+$/.test(addr)

  if (isV4) {
    // Cada octeto tem que caber em 0–255. É o erro nº 1 de planilha exportada
    // (um "300" que ninguém revisou) e o backend o recusaria depois do upload.
    if (addr.split(".").some((o) => Number(o) > 255)) return false
    if (prefix !== undefined) {
      const p = Number(prefix)
      if (!Number.isInteger(p) || p < 0 || p > 32) return false
    }
    return true
  }
  if (isV6) {
    if (prefix !== undefined) {
      const p = Number(prefix)
      if (!Number.isInteger(p) || p < 0 || p > 128) return false
    }
    return true
  }
  return false
}

export interface RowIssue {
  /** Número da linha no ARQUIVO (1 é o cabeçalho), para o operador achar. */
  line: number
  key: string
  reason: "invalid_key" | "empty_key" | "duplicate_key"
}

export interface BuildResult {
  rows: Record<string, Record<string, string>>
  issues: RowIssue[]
  /** Chaves que já existiam na versão vigente com valor DIFERENTE. */
  changed: string[]
  added: string[]
  removed: string[]
}

/**
 * Monta o corpo `{chave: {campo: valor}}` e o diff contra a versão vigente.
 *
 * O diff não é enfeite: publicar substitui a versão inteira, então "12 linhas
 * novas e 2 removidas" é a única forma de o operador perceber que exportou o
 * arquivo errado ANTES de publicar — e não depois, quando a regra parar de
 * casar em produção.
 */
export function buildRows(
  parsed: ParsedCsv,
  keyColumn: string,
  valueColumns: string[],
  matchMode: "exact" | "cidr",
  current: Record<string, Record<string, unknown>> = {},
): BuildResult {
  const rows: Record<string, Record<string, string>> = {}
  const issues: RowIssue[] = []
  const seen = new Set<string>()

  parsed.rows.forEach((row, idx) => {
    const line = idx + 2 // +1 pelo cabeçalho, +1 porque humanos contam de 1
    const key = (row[keyColumn] ?? "").trim()
    if (!key) {
      issues.push({ line, key, reason: "empty_key" })
      return
    }
    if (matchMode === "cidr" && !isValidCidrOrIp(key)) {
      issues.push({ line, key, reason: "invalid_key" })
      return
    }
    if (seen.has(key)) {
      // A última ocorrência venceria em silêncio. Dizer qual linha duplica é o
      // que permite corrigir a planilha em vez de descobrir pelo dado errado.
      issues.push({ line, key, reason: "duplicate_key" })
      return
    }
    seen.add(key)
    const value: Record<string, string> = {}
    for (const col of valueColumns) value[col] = row[col] ?? ""
    rows[key] = value
  })

  const currentKeys = new Set(Object.keys(current))
  const nextKeys = new Set(Object.keys(rows))
  const added = [...nextKeys].filter((k) => !currentKeys.has(k))
  const removed = [...currentKeys].filter((k) => !nextKeys.has(k))
  const changed = [...nextKeys].filter(
    (k) =>
      currentKeys.has(k) &&
      JSON.stringify(current[k] ?? {}) !== JSON.stringify(rows[k]),
  )

  return { rows, issues, added, removed, changed }
}

/** Estimativa do tamanho residente, para comparar com o teto por tabela. */
export function approxBytes(rows: Record<string, Record<string, string>>): number {
  return new Blob([JSON.stringify(rows)]).size
}
