import { expect, type Locator, type Page } from "@playwright/test";

/**
 * Botão "Editar mapping sophos/sophos.alert" da listagem de /mappings, com a
 * lista já filtrada pela busca.
 *
 * A listagem pagina em 15 e ordena por vendor. Cada coletor novo (azure, gcp,
 * akamai, google_workspace, microsoft_365...) empurrava o sophos para a página 2,
 * e o clique esperava 30 s por um botão que não estava na tela. Filtrar pela
 * busca deixa o teste independente de quantos mappings o produto tem.
 *
 * Falha (não pula) quando o botão não aparece: um `test.skip()` aqui deixava os
 * testes de RBAC verdes sem verificar permissão nenhuma.
 */
export async function botaoEditarSophosAlert(page: Page): Promise<Locator> {
  await page.getByTestId("mappings-search").fill("sophos.alert");
  const botao = page
    .getByRole("button", { name: /editar mapping sophos\/sophos\.alert/i })
    .first();
  await expect(botao).toBeVisible({ timeout: 10_000 });
  return botao;
}
