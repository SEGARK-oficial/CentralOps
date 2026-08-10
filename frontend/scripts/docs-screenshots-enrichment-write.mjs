/**
 * Captura as telas de ESCRITA do enriquecimento (criar tabela, editor de
 * regras + dry-run) para a doc. Complementa docs-screenshots.mjs, que só
 * cobre as telas de leitura.
 *
 * Mesma exigência: instância local no ar e SEMEADA — em particular, precisa
 * de uma tabela `rede-corporativa` com versão publicada e uma política
 * qualquer (ver docs/enrichment/how-to-enrich.md, passos 2–4).
 *
 *   node scripts/docs-screenshots-enrichment-write.mjs ../docs-site/static/img/console
 */
import { chromium } from "playwright"
import { mkdirSync } from "node:fs"

const BASE = process.env.BASE ?? "http://localhost:3200"
const USER = process.env.ADMIN_USER ?? "admin"
const PASS = process.env.ADMIN_PASS ?? "AdminPassword123!"
const OUT = process.argv[2] ?? "./shots"

mkdirSync(OUT, { recursive: true })
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const browser = await chromium.launch()
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  locale: "pt-BR",
})
const page = await ctx.newPage()

// Seletores por atributo (type), não por texto: o app troca o placeholder/
// label conforme o idioma detectado do navegador, não o locale do contexto.
await page.goto(`${BASE}/`, { waitUntil: "networkidle" })
await page.locator('input[type="text"]').first().fill(USER)
await page.locator('input[type="password"]').first().fill(PASS)
await page.locator('button[type="submit"]').first().click()
await page.waitForURL(/dashboard|\/$/, { timeout: 15000 }).catch(() => {})
await sleep(1200)
await page.evaluate(() => localStorage.setItem("centralops_org_id", "1"))

// ── 1. Nova tabela (modal preenchido, sem submeter) ─────────────────────────
await page.goto(`${BASE}/enrichment`, { waitUntil: "domcontentloaded" })
await sleep(1000)
await page.getByRole("tab", { name: /Tables|Tabelas/i }).click()
await sleep(400)
await page.getByRole("button", { name: /New table|Nova tabela/i }).click()
await sleep(400)
const createTableDialog = page.getByRole("dialog")
await createTableDialog.getByRole("button", { name: /Organization|Organização/i }).click()
await sleep(200)
await page.getByRole("option").first().click()
await createTableDialog.getByPlaceholder("rede-corporativa").fill("hosts-vip")
await createTableDialog.getByLabel(/Description|Descrição/i).fill("Hosts com prioridade de resposta no incidente")
await createTableDialog.getByRole("button", { name: /Key type|Tipo de chave/i }).click()
await sleep(200)
await page.getByRole("option", { name: "user" }).click()
await sleep(300)
await page.screenshot({ path: `${OUT}/console-enriquecimento-nova-tabela.png` })
console.log("ok  console-enriquecimento-nova-tabela")
await page.keyboard.press("Escape")
await sleep(300)

// ── 2. Editor de regras + dry-run (modal de versões da política) ───────────
await page.goto(`${BASE}/enrichment`, { waitUntil: "domcontentloaded" })
await sleep(1000)
await page.getByRole("tab", { name: /Policies|Políticas/i }).click()
await sleep(400)
await page.locator('[data-testid^="policy-card-"]').first().click()
await sleep(500)

await page.getByTestId("add-rule").click()
await sleep(300)

// Enricher = a fonte de tabela local; Tabela = rede-corporativa (tem dados publicados).
await page.getByLabel(/Enricher/i).click()
await sleep(200)
await page.getByRole("option", { name: /table_cidr/i }).click()
await sleep(200)
await page.getByRole("button", { name: /^Table$|^Tabela$/i }).click()
await sleep(200)
await page.getByRole("option", { name: "rede-corporativa" }).click()
await sleep(300)

// A regra nasce com outputs[0].from vazio (só o target já vem pré-preenchido
// com o prefixo) — sem isso o dry-run rejeita com 'from' deve ser string não-vazia.
await page.getByRole("textbox", { name: "Campo do resultado" }).fill("site")
await page.getByRole("textbox", { name: "Grava em" }).fill("_centralops.enrichment.src.site")
await sleep(200)

// Tabelas simuladas: sem preencher, o campo mostra só o placeholder (texto
// cinza) e a chamada vai sem `tables` — preenche de verdade, usando o id real
// da regra (gerado pelo componente, ex.: "regra-1").
const ruleId = await page.getByRole("textbox", { name: "Id da regra" }).inputValue()
await page
  .getByRole("textbox", { name: /Tabelas simuladas/ })
  .fill(JSON.stringify({ [ruleId]: { "10.0.5.7": { site: "filial-sp" } } }))
await sleep(200)

await page.getByRole("button", { name: /^Test$|^Testar$/i }).click()
await sleep(1200)
// O resultado nasce fora da viewport (dentro do scroll interno do modal) —
// fullPage não ajuda aqui, é o container interno que precisa rolar.
await page.getByTestId("dry-run-result").scrollIntoViewIfNeeded()
await sleep(300)
await page.screenshot({ path: `${OUT}/console-enriquecimento-editor-regras-dryrun.png` })
console.log("ok  console-enriquecimento-editor-regras-dryrun")

await browser.close()
console.log("\nfim")
