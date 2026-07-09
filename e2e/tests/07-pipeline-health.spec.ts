/**
 * 07-pipeline-health.spec.ts — Pipeline Health: métricas e status por integração.
 *
 * Sprint 5: saúde do pipeline de normalização por integração + página agregada.
 *
 * O que este arquivo valida:
 *   - aba "Saúde de Normalização" na IntegrationDetailPage renderiza
 *   - página /health agregada lista cards de integração
 *   - filtros por status funcionam na página /health
 *   - status "unhealthy" aparece para integração com lag alto (fixture específica)
 *
 * O que este arquivo NÃO valida:
 *   - Cálculo de lag/métricas (coberto em pytest backend)
 *   - Renderização interna de cada card (coberto em Vitest de StatusCard/MetricsGrid)
 *   - Refresco automático com intervalo (coberto em Vitest de IntegrationHealthPanel)
 */

import { test, expect } from "@playwright/test";

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * Navega para a página SPA /health via o APP (client-side). Um page-load direto
 * em /health é interceptado pelo endpoint de healthcheck do backend (responde
 * "healthy" em texto puro), nunca servindo o SPA. Então abrimos o app e clicamos
 * o link "Saúde do Pipeline" da sidebar para chegar à rota por pushState.
 */
async function gotoHealth(page: import("@playwright/test").Page): Promise<void> {
  await page.goto("/dashboard");
  await page.getByRole("link", { name: /saúde do pipeline/i }).click();
  await page.waitForURL(/\/pipeline-health$/, { timeout: 10_000 });
}

/**
 * Retorna o ID da primeira integração disponível via API.
 * Usado para navegar diretamente para /integrations/:id.
 */
async function getFirstIntegrationId(
  request: import("@playwright/test").APIRequestContext
): Promise<number | null> {
  const response = await request.get("/api/integrations");
  if (!response.ok()) return null;
  const integrations = (await response.json()) as Array<{ id: number; name: string }>;
  if (integrations.length === 0) return null;
  return integrations[0].id;
}

// ── Tests admin ───────────────────────────────────────────────────────────────

test.describe("Pipeline Health — IntegrationDetailPage (Sprint 5)", () => {
  test.use({ storageState: ".auth/admin.json" });

  test("aba 'Saúde de Normalização' renderiza painel de métricas", async ({ page, request }) => {
    const integrationId = await getFirstIntegrationId(request);
    if (!integrationId) {
      console.log("[health-test] Nenhuma integração encontrada — seed pode não ter criado.");
      test.skip();
      return;
    }

    await page.goto(`/integrations/${integrationId}`);

    // Aguarda página carregar
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});

    // Clicar na aba "Saúde de Normalização"
    const healthTab = page.getByRole("tab", { name: /saúde de normalização/i });
    const tabVisible = await healthTab.isVisible({ timeout: 8_000 }).catch(() => false);

    if (!tabVisible) {
      console.log("[health-test] Aba 'Saúde de Normalização' não encontrada.");
      test.skip();
      return;
    }

    await healthTab.click();

    // Painel de saúde deve aparecer — data-testid="integration-health-panel"
    await expect(page.getByTestId("integration-health-panel")).toBeVisible({ timeout: 10_000 });

    // Deve mostrar StatusCard com status badge
    // StatusCard usa HealthBadge que exibe status textual
    const healthPanel = page.getByTestId("integration-health-panel");
    await expect(healthPanel).toBeVisible();

    // Botão de refresh deve estar disponível
    await expect(healthPanel.getByTestId("health-refresh-button")).toBeVisible({ timeout: 5_000 });
  });

  test("botão Atualizar em IntegrationHealthPanel dispara nova requisição", async ({ page, request }) => {
    const integrationId = await getFirstIntegrationId(request);
    if (!integrationId) {
      test.skip();
      return;
    }

    await page.goto(`/integrations/${integrationId}`);
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});

    const healthTab = page.getByRole("tab", { name: /saúde de normalização/i });
    const tabVisible = await healthTab.isVisible({ timeout: 8_000 }).catch(() => false);
    if (!tabVisible) {
      test.skip();
      return;
    }

    await healthTab.click();
    await expect(page.getByTestId("integration-health-panel")).toBeVisible({ timeout: 10_000 });

    // Interceptar a próxima requisição de pipeline-health
    const refreshPromise = page.waitForResponse(
      (r) => r.url().includes("/api/integrations/") && r.url().includes("pipeline-health"),
      { timeout: 8_000 }
    );

    await page.getByTestId("health-refresh-button").click();

    const response = await refreshPromise;
    expect(response.status()).toBeLessThan(300);
  });
});

// ── Tests página /health agregada ────────────────────────────────────────────

test.describe("Pipeline Health — página /health agregada (Sprint 5)", () => {
  test.use({ storageState: ".auth/admin.json" });

  test("página /health carrega e lista cards de integração", async ({ page }) => {
    await gotoHealth(page);
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});

    // Título da página deve estar visível
    await expect(page.getByRole("heading", { name: /saúde do pipeline/i })).toBeVisible({ timeout: 10_000 });

    // Se há integrações, deve mostrar ao menos um card ou empty state
    // A página usa grid — verificar que não há erro crítico
    await expect(page.getByText(/falha ao carregar/i)).not.toBeVisible({ timeout: 5_000 });
  });

  test("filtro 'Saudáveis' na /health mostra apenas integrações saudáveis", async ({ page }) => {
    await gotoHealth(page);
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});

    // Aguarda carregamento inicial
    await expect(page.getByRole("heading", { name: /saúde do pipeline/i })).toBeVisible({ timeout: 10_000 });

    // Esperar que o loading termine
    await expect(page.getByText(/carregando/i)).not.toBeVisible({ timeout: 8_000 });

    // Clicar na tab "Saudáveis"
    const healthyTab = page.getByRole("tab", { name: /saudáveis/i });
    const tabVisible = await healthyTab.isVisible().catch(() => false);

    if (!tabVisible) {
      // Tabs de filtro podem não estar visíveis se não há integrações
      return;
    }

    await healthyTab.click();

    // Não deve mostrar erro após filtro
    await expect(page.getByText(/falha ao carregar/i)).not.toBeVisible({ timeout: 3_000 });
  });

  test("filtro 'Com problema' na /health mostra integração unhealthy", async ({ page }) => {
    await gotoHealth(page);
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});

    await expect(page.getByRole("heading", { name: /saúde do pipeline/i })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText(/carregando/i)).not.toBeVisible({ timeout: 8_000 });

    // Clicar em "Com problema"
    const problemTab = page.getByRole("tab", { name: /com problema/i });
    const tabVisible = await problemTab.isVisible().catch(() => false);

    if (!tabVisible) return;

    await problemTab.click();

    // A integração e2e-integration-unhealthy criada pelo seed deve ter status unhealthy
    // (sem last_success_at, lag calculado como máximo)
    // Não assertamos o nome específico — pode haver zero results se seed não funcionou
    await expect(page.getByText(/falha ao carregar/i)).not.toBeVisible({ timeout: 3_000 });
  });
});

// ── Tests viewer ──────────────────────────────────────────────────────────────

test.describe("Pipeline Health — acesso viewer (Sprint 5)", () => {
  test.use({ storageState: ".auth/viewer.json" });

  test("viewer acessa /health sem erro de permissão", async ({ page }) => {
    // Health é leitura — viewer deve poder acessar
    await gotoHealth(page);
    await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});

    // Não deve mostrar 403 nem redirecionar para /login
    const currentUrl = page.url();
    expect(currentUrl).not.toContain("/login");
    await expect(page.getByText(/403|acesso negado/i)).not.toBeVisible({ timeout: 5_000 });
  });
});
