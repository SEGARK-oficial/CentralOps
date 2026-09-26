#!/usr/bin/env node
/**
 * Catraca (ratchet) de literais em PT fora do `t()` — R2-9.3.
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
 * Catraca: cada achado vira uma ASSINATURA (arquivo:linha:trecho) e o
 * baseline (`scripts/i18n-literals-baseline.json`, versionado) é a lista de
 * assinaturas TOLERADAS hoje. O gate falha se aparecer uma assinatura NOVA
 * (não está no baseline) — reduzir o baseline (corrigir e apagar a linha) é
 * sempre permitido; a catraca só trava contra CRESCER.
 *
 * Varre `src/` (Core) e, se existir, `web-ee/` (overlay EE, sincronizado
 * localmente só durante a checagem — nunca versionado aqui).
 *
 * Puro Node, sem deps. Rodado via `npm run i18n:check` (ver scripts/check-i18n.mjs).
 */
import { readFileSync, existsSync, writeFileSync } from "node:fs"
import { join, dirname, relative } from "node:path"
import { fileURLToPath } from "node:url"
import { findingsInText, walkTsFiles } from "./lib/i18n-literals-core.mjs"

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
let baseline = []
try {
  baseline = JSON.parse(readFileSync(BASELINE_PATH, "utf8"))
} catch {
  baseline = []
}
const baselineSet = new Set(baseline)
const currentSignatures = findings.map((f) => f.signature)
const currentSet = new Set(currentSignatures)

const novos = findings.filter((f) => !baselineSet.has(f.signature))

if (UPDATE_MODE) {
  const next = [...currentSet].sort()
  writeFileSync(BASELINE_PATH, JSON.stringify(next, null, 2) + "\n")
  console.log(`Baseline de literais PT atualizado: ${baseline.length} → ${next.length} entrada(s).`)
  process.exit(0)
}

if (novos.length > 0) {
  fail(`${novos.length} literal(is) em PT NOVO(s), fora do baseline (rode "node scripts/check-i18n-literals.mjs --update-baseline" só depois de migrar para t(), nunca pra silenciar):`)
  for (const f of novos.slice(0, 20)) {
    console.error(`      ${f.signature}`)
  }
  if (novos.length > 20) console.error(`      … e mais ${novos.length - 20}`)
}

const reduzidos = baseline.length - [...baselineSet].filter((s) => currentSet.has(s)).length

console.log(
  errors === 0
    ? `✅ catraca de literais PT OK — ${filesScanned} arquivo(s) varrido(s), ${findings.length} achado(s) (baseline: ${baseline.length}${reduzidos > 0 ? `, ${reduzidos} reduzido(s) desde o baseline` : ""})`
    : `\n❌ ${errors} problema(s) na catraca de literais PT`,
)
process.exit(errors === 0 ? 0 : 1)
