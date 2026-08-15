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
  // O orgId vem do seed.ts (e2e/.e2e-org-id): a MESMA org sob a qual o
  // seed-redis-e2e.sh populou o reservoir. cwd=e2e/ (CJS e ESM ok).
  const orgId = readFileSync(".e2e-org-id", "utf8").trim();

  // O filtro de org precisa estar posto ANTES do dry-run: o reservoir é por-org
  // e o admin é GLOBAL (org=None), então sem nomear o tenant a leitura é
  // fail-closed e devolve amostra vazia (não erro), o que faz o envelope sair
  // sem `class_uid` e este teste falhar por um motivo que não é o dele.
  //
  // Semear o localStorage antes do primeiro load NÃO funciona mais. O
  // PlatformContext reconcilia a seleção guardada com a IDENTIDADE do dono
  // (`centralops_scope_owner`): quando a marca está ausente, ele assume que a
  // seleção pode ter sobrado de outro usuário e limpa, o que é deliberado e
  // existe para não reproduzir o 403 que travava o dashboard depois de trocar
  // de conta.
  //
  // Então a ordem aqui é: carregar uma vez para o app carimbar a marca, gravar
  // o filtro, e recarregar. No segundo load a marca casa e a seleção sobrevive.
  // Fazer assim também evita depender do id do usuário, que o teste não conhece.
  //
  // O `waitForFunction` NÃO é enfeite. O efeito de reconciliação começa com
  // `if (!userId) return`, e o `goto` resolve no evento de load, antes de a
  // autenticação terminar. Gravar o filtro logo após o `goto` cai numa corrida:
  // a auth resolve depois, o efeito roda, não acha a marca e limpa justamente o
  // que o teste acabou de gravar. Esperar a marca APARECER é o sinal de que o
  // efeito já rodou e não vai rodar de novo para este usuário.
  await page.goto("/mappings");
  await page.waitForFunction(
    () => window.localStorage.getItem("centralops_scope_owner") !== null,
    undefined,
    { timeout: 15_000 },
  );
  await page.evaluate((id) => {
    window.localStorage.setItem("centralops_org_id", id);
  }, orgId);
  await page.reload();

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
