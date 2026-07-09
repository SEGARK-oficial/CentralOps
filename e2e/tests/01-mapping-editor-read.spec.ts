/**
 * 01-mapping-editor-read.spec.ts — Mapping Editor em modo leitura.
 *
 * Sprint 1 POC: valida que o MappingEditorPage renderiza os 3 painéis
 * (payload, regras, envelope) ao navegar para um mapping existente.
 *
 * ESTADO ATUAL (Sprint 1 em andamento):
 *   - Rota /mappings NÃO existe no frontend ainda — esses testes vão
 *     falhar com timeout até o Sprint 1 frontend ser mergeado.
 *   - Isso é ESPERADO. Ver README.md seção "Quando os testes vão passar".
 *
 * O que este teste VALIDA:
 *   - Tabela de mappings renderiza na rota /mappings
 *   - Link para mapping com event_type "sophos.alert" está visível
 *   - Painel de payload (esquerda) está presente
 *   - Painel de regras (centro) está presente
 *   - Painel de envelope (direita) está presente
 *
 * O que este teste NÃO VALIDA:
 *   - Conteúdo interno de cada painel (JSON, campos específicos)
 *   - Comportamento de edição (Sprint 2)
 *   - Permissões por role (Sprint 4)
 *   - Performance de renderização
 *
 * Convenção de selectors: preferir getByRole e getByLabel.
 * data-testid apenas como fallback quando role/aria não são suficientes.
 * Ver README.md seção "Convenção de selectors".
 */

import { test, expect } from "@playwright/test";

test.describe("Mapping Editor — read mode (Sprint 1)", () => {
  test("renderiza 3 paineis ao abrir mapping existente", async ({ page }) => {
    // Navegar para a listagem de mappings
    await page.goto("/mappings");

    // Aguarda a tabela aparecer — prova que a rota existe e a API respondeu
    await expect(page.getByRole("table")).toBeVisible({ timeout: 10_000 });

    // Abrir o mapping sophos/sophos.alert. A listagem navega por um <button>
    // (aria-label "Editar mapping <vendor>/<event_type>"), não por <a>/link.
    await page.getByRole("button", { name: /editar mapping sophos\/sophos\.alert/i }).first().click();

    // ── Validar os 3 painéis do editor ──────────────────────────────────

    // Painel esquerdo: payload raw do evento
    // ARIA: <section aria-label="Payload" role="region">
    await expect(
      page.getByRole("region", { name: /payload/i })
    ).toBeVisible({ timeout: 10_000 });

    // Painel central: lista de regras de mapeamento
    // ARIA: <section aria-label="Regras" role="region">
    await expect(
      page.getByRole("region", { name: /regras/i })
    ).toBeVisible({ timeout: 10_000 });

    // Painel direito: envelope OCSF gerado pelo dry-run
    // ARIA: <section aria-label="Envelope OCSF" role="region">
    await expect(
      page.getByRole("region", { name: /envelope/i })
    ).toBeVisible({ timeout: 10_000 });
  });
});
