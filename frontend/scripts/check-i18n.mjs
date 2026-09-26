#!/usr/bin/env node
/**
 * i18n guard (ADR i18n Fase 5) — fails CI on the two regressions that actually
 * bit us during the rollout:
 *   1. KEY PARITY — a key present in one locale's catalog but missing in another
 *      (e.g. an agent trimming en/es of `common.json`). Every namespace must have
 *      the identical key set across pt / en / es.
 *   2. DANGLING t() — a component calls t("some.key") that has no catalog entry,
 *      so it would render the raw key at runtime.
 *
 * A extração do(s) namespace(s) default e a resolução de `t()` contra eles
 * vivem em `scripts/lib/i18n-check-core.mjs` (puro, sem I/O) — testado em
 * `src/__tests__/i18n-check-core.test.ts` (inclui `useTranslation([...])`
 * com array, positivo e negativo).
 *
 * Pure Node, no deps. Run: `node scripts/check-i18n.mjs` (npm run i18n:check).
 */
import { readdirSync, readFileSync, statSync } from "node:fs"
import { join, dirname } from "node:path"
import { fileURLToPath } from "node:url"
import { flatten, extractDefaultNamespaces, resolveNamespaceCandidates, keyResolvesInAnyNamespace } from "./lib/i18n-check-core.mjs"

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..")
const LOCALES_DIR = join(ROOT, "src", "i18n", "locales")
const SRC_DIR = join(ROOT, "src")
const LOCALES = ["pt", "en", "es"]

let errors = 0
const fail = (msg) => {
  console.error(`  ✗ ${msg}`)
  errors++
}

// ── 1. Key parity across locales, per namespace ──────────────────────────────
const namespaces = readdirSync(join(LOCALES_DIR, "pt"))
  .filter((f) => f.endsWith(".json"))
  .map((f) => f.replace(/\.json$/, ""))

const catalogs = {} // { ns: { locale: Set<key> } }
for (const ns of namespaces) {
  catalogs[ns] = {}
  for (const loc of LOCALES) {
    const path = join(LOCALES_DIR, loc, `${ns}.json`)
    try {
      catalogs[ns][loc] = flatten(JSON.parse(readFileSync(path, "utf8")))
    } catch (e) {
      fail(`${loc}/${ns}.json: ${e.message}`)
      catalogs[ns][loc] = new Set()
    }
  }
  const [pt, en, es] = LOCALES.map((l) => catalogs[ns][l])
  for (const [loc, set] of [["en", en], ["es", es]]) {
    const missing = [...pt].filter((k) => !set.has(k))
    const extra = [...set].filter((k) => !pt.has(k))
    if (missing.length) fail(`${ns}: ${loc} is MISSING ${missing.length} key(s): ${missing.slice(0, 5).join(", ")}${missing.length > 5 ? " …" : ""}`)
    if (extra.length) fail(`${ns}: ${loc} has ${extra.length} EXTRA key(s) not in pt: ${extra.slice(0, 5).join(", ")}`)
  }
}

// ── 2. Every static t("key") resolves to a catalog entry ─────────────────────
const getCatalogKeys = (ns) => catalogs[ns]?.pt

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (name === "node_modules" || name === "i18n" || name.startsWith(".")) continue
    if (statSync(p).isDirectory()) yield* walk(p)
    else if (/\.tsx?$/.test(name) && !/\.test\.|__tests__/.test(p)) yield p
  }
}

let checked = 0
for (const file of walk(SRC_DIR)) {
  const txt = readFileSync(file, "utf8")
  const defNs = extractDefaultNamespaces(txt)
  for (const m of txt.matchAll(/\bt\(\s*"([^"]+)"/g)) {
    const raw = m[1]
    checked++
    const { nsList, key } = resolveNamespaceCandidates(raw, defNs)
    if (!keyResolvesInAnyNamespace(nsList, key, getCatalogKeys))
      fail(`${file.replace(ROOT + "/", "")}: t("${raw}") [ns=${nsList.join("|")}] → no catalog entry`)
  }
}

console.log(
  errors === 0
    ? `✅ i18n OK — ${namespaces.length} namespaces × ${LOCALES.length} locales in parity; ${checked} t() calls all resolve`
    : `\n❌ ${errors} i18n issue(s) found`,
)
process.exit(errors === 0 ? 0 : 1)
