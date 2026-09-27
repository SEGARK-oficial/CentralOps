#!/usr/bin/env node
/**
 * Catraca (ratchet) de literais em PT fora do `t()` — R2-9.3 / R3-9.2.
 *
 * `check-i18n.mjs` prova que todo `t("chave")` RESOLVE. Este script prova a
 * metade oposta: nenhum texto visível, `aria-label`, `title`, `placeholder`
 * ou `alt` está com PROSA CRUA em PT no JSX, contornando o i18n inteiro (o
 * bug fica invisível pros dois outros gates — parity e resolução — porque
 * não existe chamada a `t()` nenhuma pra checar).
 *
 * A heurística e a varredura de arquivo vivem em `scripts/lib/i18n-literals-
 * core.mjs` (puro, sem I/O de CLI) — compartilhado com o meta-teste em
 * `src/__tests__/i18n-literals-guard.test.ts`, que prova que o detector
 * PEGA um literal injetado antes de confiar no resultado "zero" abaixo.
 *
 * Catraca: cada achado vira uma ASSINATURA `arquivo:trecho` (SEM linha — ver
 * R3-9.2 abaixo) e o baseline (`scripts/i18n-literals-baseline.json`,
 * versionado) é um MAPA assinatura→contagem tolerada hoje. O gate falha se a
 * contagem ATUAL de alguma assinatura exceder a do baseline — reduzir
 * (corrigir e diminuir/apagar a entrada) é sempre permitido.
 *
 * R3-9.2 — por que SEM linha: um baseline por linha (`arquivo:linha:trecho`)
 * quebra a cada edição ACIMA do achado, sem nenhum literal novo — foi o que
 * aconteceu na Rodada 3 (`Tabs.tsx:43` → `:44` só por causa de uma linha
 * inserida acima, exigindo reajuste manual). Contar por (arquivo, trecho) em
 * vez de checar só presença/ausência é o que permite pegar uma OCORRÊNCIA
 * NOVA do mesmo texto (ex.: a mesma frase colada de novo alhures no mesmo
 * arquivo) mesmo com a assinatura já "conhecida".
 *
 * Varre `src/` (Core) e, se existir, `web-ee/` (overlay EE, sincronizado
 * localmente só durante a checagem — nunca versionado aqui).
 *
 * Puro Node, sem deps. Rodado via `npm run i18n:check` (ver scripts/check-i18n.mjs).
 */
import { readFileSync, existsSync, writeFileSync } from "node:fs"
import { join, dirname, relative } from "node:path"
import { fileURLToPath } from "node:url"
import {
  findingsInText,
  walkTsFiles,
  countBySignature,
  findOverages,
  countsToBaselineObject,
  baselineToCounts,
} from "./lib/i18n-literals-core.mjs"

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..")
const BASELINE_PATH = join(ROOT, "scripts", "i18n-literals-baseline.json")
const ROOTS = [join(ROOT, "src"), join(ROOT, "web-ee")].filter(existsSync)

const UPDATE_MODE = process.argv.includes("--update-baseline")

let errors = 0
const fail = (msg) => {
  console.error(`  ✗ ${msg}`)
  errors++
}

let filesScanned = 0
const findings = []
for (const root of ROOTS) {
  for (const file of walkTsFiles(root)) {
    filesScanned++
    findings.push(...findingsInText(readFileSync(file, "utf8"), relative(ROOT, file)))
  }
}

// ── Anti-vacuidade ────────────────────────────────────────────────────────────
if (filesScanned < 50) {
  fail(`o varredor de literais só percorreu ${filesScanned} arquivo(s) — esperado bem mais; caminho ou regex quebrados?`)
}

// ── Baseline ──────────────────────────────────────────────────────────────────
let baselineRaw = {}
try {
  baselineRaw = JSON.parse(readFileSync(BASELINE_PATH, "utf8"))
} catch {
  baselineRaw = {}
}
const baselineCounts = baselineToCounts(baselineRaw)
const currentCounts = countBySignature(findings)

if (UPDATE_MODE) {
  writeFileSync(BASELINE_PATH, JSON.stringify(countsToBaselineObject(currentCounts), null, 2) + "\n")
  console.log(`Baseline de literais PT atualizado: ${baselineCounts.size} → ${currentCounts.size} assinatura(s).`)
  process.exit(0)
}

const overages = findOverages(currentCounts, baselineCounts)

if (overages.length > 0) {
  const totalExtra = overages.reduce((sum, o) => sum + o.extra, 0)
  fail(`${totalExtra} literal(is) em PT NOVO(s) (${overages.length} assinatura(s) acima do baseline) — rode "node scripts/check-i18n-literals.mjs --update-baseline" só depois de migrar para t(), nunca pra silenciar:`)
  // Reaproveita a 1ª ocorrência de cada assinatura estourada só para apontar
  // um arquivo:linha legível no erro — a comparação em si NUNCA usa a linha.
  const exampleLineFor = new Map()
  for (const f of findings) if (!exampleLineFor.has(f.signature)) exampleLineFor.set(f.signature, f.line)
  for (const o of overages.slice(0, 20)) {
    const line = exampleLineFor.get(o.signature)
    console.error(`      ${o.signature} (linha ${line}, ${o.current}× atual / ${o.baseline}× no baseline)`)
  }
  if (overages.length > 20) console.error(`      … e mais ${overages.length - 20}`)
}

let reduzidos = 0
for (const [signature, baseline] of baselineCounts) {
  const current = currentCounts.get(signature) ?? 0
  if (current < baseline) reduzidos += baseline - current
}

console.log(
  errors === 0
    ? `✅ catraca de literais PT OK — ${filesScanned} arquivo(s) varrido(s), ${findings.length} achado(s) (baseline: ${[...baselineCounts.values()].reduce((a, b) => a + b, 0)}${reduzidos > 0 ? `, ${reduzidos} reduzido(s) desde o baseline` : ""})`
    : `\n❌ ${errors} problema(s) na catraca de literais PT`,
)
process.exit(errors === 0 ? 0 : 1)
