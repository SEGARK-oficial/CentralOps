/**
 * Captura as telas do console para a documentação (docs-site/static/img/console/).
 *
 * Rodar daqui, e não de docs-site/, porque o Playwright está instalado no
 * node_modules do frontend:
 *
 *   node scripts/docs-screenshots.mjs ../docs-site/static/img/console
 *
 * Exige uma instância local no ar e SEMEADA (organizações, integrações, destinos,
 * rotas). Contra uma instância vazia os prints saem em estado vazio, que é o
 * oposto do que a doc precisa mostrar.
 *
 * Roda contra a instância local semeada (organizações, integrações, destinos,
 * rotas e amostras no reservoir). Nada aqui inventa dado: o que aparece no print
 * é o que a aplicação renderiza.
 *
 * deviceScaleFactor 2 porque a doc é lida em tela retina — em 1x o texto do
 * console, que é denso e pequeno, sai borrado e parece amador.
 */
import { chromium } from "playwright"
import { mkdirSync } from "node:fs"

const BASE = process.env.BASE ?? "http://localhost:3200"
const USER = process.env.ADMIN_USER ?? "admin"
const PASS = process.env.ADMIN_PASS ?? "RedesignLocal2026"
const OUT = process.argv[2] ?? "./shots"

mkdirSync(OUT, { recursive: true })

/** Telas a capturar: [arquivo, rota, opções] */
const SCREENS = [
  ["console-dashboard", "/dashboard", {}],
  ["console-integracoes", "/integrations", {}],
  ["console-rotas", "/routes", {}],
  ["console-destinos", "/destinations", {}],
  ["console-fluxo", "/flow", {}],
  ["console-saude-pipeline", "/pipeline-health", {}],
  ["console-mapeamentos", "/mappings", {}],
  ["console-editor-mapping", "/mappings/__MAPPING_ID__", { wait: 2500 }],
]

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const browser = await chromium.launch()
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  locale: "pt-BR",
})
const page = await ctx.newPage()

// ── Login ────────────────────────────────────────────────────────────────────
await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" })

// A tela de login é a primeira captura, e vale mostrá-la ANTES de preencher.
await sleep(800)
await page.screenshot({ path: `${OUT}/console-login.png` })
console.log("ok  console-login")

await page.getByRole("textbox").first().fill(USER)
await page.locator('input[type="password"]').fill(PASS)
await page.getByRole("button", { name: /entrar|sign in/i }).click()
await page.waitForURL(/\/(dashboard|)$/, { timeout: 15000 }).catch(() => {})
await sleep(1500)

// O reservoir e os KPIs são por organização: sem escolher uma, o admin global vê
// tudo zerado e os prints ficariam vazios — que é justamente o contrário do que
// a doc precisa mostrar.
await page.evaluate(() => localStorage.setItem("centralops_org_id", "1"))

// Descobre o id do mapping sophos.alert (o que tem amostras no reservoir).
const mappingId = await page.evaluate(async () => {
  const r = await fetch("/api/mappings", { credentials: "include" })
  const d = await r.json()
  const items = Array.isArray(d) ? d : (d.items ?? [])
  return items.find((m) => m.event_type === "sophos.alert")?.id ?? items[0]?.id ?? ""
})
console.log("mapping do editor:", mappingId || "(nenhum)")

for (const [name, route, opts] of SCREENS) {
  const url = route.replace("__MAPPING_ID__", mappingId)
  if (url.includes("__MAPPING_ID__") || (route.includes("__MAPPING_ID__") && !mappingId)) {
    console.log("pulado", name, "(sem mapping)")
    continue
  }
  await page.goto(`${BASE}${url}`, { waitUntil: "domcontentloaded" })
  await sleep(opts.wait ?? 1400)
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log("ok ", name)
}

// ── Sidebar isolada ──────────────────────────────────────────────────────────
// A navegação por estágio do pipeline é a mudança que mais afeta a doc: vale uma
// imagem só dela, para os tutoriais poderem apontar "o grupo X" sem descrever.
await page.goto(`${BASE}/dashboard`, { waitUntil: "domcontentloaded" })
await sleep(1200)
const nav = page.locator("#primary-navigation")
if (await nav.count()) {
  await nav.first().screenshot({ path: `${OUT}/console-navegacao.png` })
  console.log("ok  console-navegacao")
}

await browser.close()
console.log("\nfim")
