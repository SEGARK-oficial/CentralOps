/**
 * 02-dry-run-live.spec.ts — Dry-run live no Mapping Editor.
 *
 * Sprint 1 POC: valida que após abrir um mapping, o dry-run automático
 * é disparado contra o backend e popula o painel direito (envelope OCSF)
 * com o campo "class_uid" esperado.
 *
 * ESTADO ATUAL (Sprint 1 em andamento):
 *   - Rota /mappings NÃO existe no frontend ainda — esses testes vão
 *     falhar até o Sprint 1 frontend ser mergeado.
 *   - Isso é ESPERADO. Ver README.md seção "Quando os testes vão passar".
 *
 * O que este teste VALIDA:
 *   - A abertura do mapping dispara uma requisição para /api/mappings/dry-run
 *   - O painel de envelope é populado com o resultado
 *   - O campo "class_uid" aparece no painel (indica que o dry-run funcionou)
 *
 * O que este teste NÃO VALIDA:
 *   - O valor correto de class_uid (isso é cobertura de unit test do engine)
 *   - Comportamento após edição de regras (Sprint 2)
 *   - Debounce do dry-run (Sprint 2)
 *   - Erros de compilação exibidos na UI (Sprint 2)
 *
 * Dependência: reservoir Redis populado com pelo menos 1 evento sintético
 * para vendor "sophos". Ver scripts/seed-redis-e2e.sh.
 */

import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

test("dry-run live atualiza painel direito apos edicao", async ({ page }) => {
  // o reservoir é por-org e o admin é GLOBAL (org=None). Para o
  // dry-run achar a amostra, o admin precisa nomear o tenant via o filtro de org
  // (selectedOrgId → localStorage centralops_org_id), que o editor repassa ao
  // /api/mappings/dry-run. O orgId vem do seed.ts (e2e/.e2e-org-id), a MESMA org
  // sob a qual o seed-redis-e2e.sh populou o reservoir. cwd=e2e/ (CJS e ESM ok).
  const orgId = readFileSync(".e2e-org-id", "utf8").trim();
  // Seta o filtro de org do admin ANTES da app carregar (PlatformContext lê o
  // localStorage na init). Sem isto o dry-run do admin global lê reservoir vazio.
  await page.addInitScript((id) => {
    window.localStorage.setItem("centralops_org_id", id);
  }, orgId);

  await page.goto("/mappings");

  // Aguarda listagem — garante que seed rodou e API está respondendo
  await expect(page.getByRole("table")).toBeVisible({ timeout: 10_000 });

  // Abrir mapping sophos/sophos.alert. A listagem navega por um <button>
  // (aria-label "Editar mapping <vendor>/<event_type>"), não por <a>/link.
  await page.getByRole("button", { name: /editar mapping sophos\/sophos\.alert/i }).first().click();
  await expect(page.getByTestId("mapping-editor-page")).toBeVisible({ timeout: 10_000 });

  // O dry-run live dispara em modo de EDIÇÃO (o painel "Reservoir" é placeholder
  // e não alimenta dry-run em view mode). Ao entrar em edit mode, o editor
  // sincroniza as regras e dispara o dry-run contra a amostra do reservoir Redis
  // (resolvida server-side). Listener montado ANTES do clique para não perder a
  // resposta por race.
  const dryRunPromise = page.waitForResponse(
    (r) => r.url().includes("/api/mappings/dry-run") && r.status() < 300,
    { timeout: 15_000 }
  );
  await page.getByTestId("edit-mode-button").click();
  await dryRunPromise;

  // O painel direito (envelope normalizado) atualiza com o resultado do dry-run.
  // A amostra do reservoir agora normaliza 100% o mapping sophos.alert (campo
  // `createdAt` satisfaz a regra required normalized.time), então o envelope OCSF
  // traz `class_uid` (regra constante 2004) — prova de dry-run bem-sucedido.
  const envelope = page.getByRole("region", { name: /envelope/i });
  await expect(envelope.getByText("class_uid")).toBeVisible({ timeout: 8_000 });
});
