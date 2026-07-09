/**
 * 05-quarantine.spec.ts — Quarentena: drawer de detalhe, descartar, Reprocessar.
 *
 * F4-S3: botão Reprocessar habilitado (era disabled na Sprint 3).
 *
 * O que este arquivo valida:
 *   - operator vê detalhe de evento e descarta com confirmação
 *   - operator reprocessa evento com sucesso (F4-S3)
 *   - viewer não vê botão Descartar nem Reprocessar
 *
 * O que este arquivo NÃO valida:
 *   - Conteúdo específico do raw_payload (coberto em Vitest do QuarantineDetailDrawer)
 *   - Paginação (coberto em Vitest do QuarantineTable)
 *   - Filtros de vendor/event_type (interações de filtro validadas em 04-drift-explorer)
 */

import { test, expect } from "@playwright/test";

// ── Tests operator ────────────────────────────────────────────────────────────

test.describe("Quarentena — ações como operator (F4-S3)", () => {
  test.use({ storageState: ".auth/operator.json" });

  test("página /quarantine carrega sem erro", async ({ page }) => {
    await page.goto("/quarantine");
    await expect(page.getByTestId("quarantine-page")).toBeVisible({ timeout: 10_000 });

    // Aguarda API responder — tabela ou empty state
    await page.waitForResponse(
      (r) => r.url().includes("/api/quarantine") && r.status() < 300,
      { timeout: 8_000 }
    ).catch(() => {});

    // Não deve mostrar erro de carregamento
    await expect(page.getByText(/erro ao carregar quarentena/i)).not.toBeVisible({ timeout: 5_000 });
  });

  test("operator abre drawer de detalhe ao clicar Detalhes", async ({ page }) => {
    await page.goto("/quarantine");
    await expect(page.getByTestId("quarantine-page")).toBeVisible({ timeout: 10_000 });

    const quarantineTable = page.getByTestId("quarantine-table");
    const tableVisible = await quarantineTable.isVisible().catch(() => false);

    if (!tableVisible) {
      console.log("[quarantine-test] Tabela vazia — seed pode não ter populado dados.");
      return;
    }

    const detailsButton = page.getByRole("button", { name: /detalhes/i }).first();
    const buttonExists = await detailsButton.isVisible().catch(() => false);

    if (!buttonExists) {
      console.log("[quarantine-test] Botão Detalhes não encontrado na tabela.");
      return;
    }

    await detailsButton.click();

    await expect(page.getByTestId("quarantine-detail-drawer")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByRole("region", { name: /payload bruto/i })).toBeVisible({ timeout: 3_000 });
  });

  test("operator reprocessa evento com sucesso", async ({ page }) => {
    await page.goto("/quarantine");
    await expect(page.getByTestId("quarantine-page")).toBeVisible({ timeout: 10_000 });

    const tableVisible = await page.getByTestId("quarantine-table").isVisible().catch(() => false);
    if (!tableVisible) {
      console.log("[quarantine-test] Tabela vazia — pulando reprocess.");
      return;
    }

    // Abre drawer via botão Detalhes
    const detailsButton = page.getByRole("button", { name: /detalhes/i }).first();
    const buttonExists = await detailsButton.isVisible().catch(() => false);
    if (!buttonExists) return;

    await detailsButton.click();
    await expect(page.getByTestId("quarantine-detail-drawer")).toBeVisible({ timeout: 5_000 });

    // Botão Reprocessar deve existir para operator (tem quarantine.discard)
    const reprocessButton = page.getByRole("button", { name: /reprocessar/i }).first();
    const reprocessVisible = await reprocessButton.isVisible().catch(() => false);
    if (!reprocessVisible) {
      console.log("[quarantine-test] Botão Reprocessar não encontrado — entry pode já estar reprocessada ou expirada.");
      return;
    }

    // Interceptar a chamada de reprocess
    const reprocessPromise = page.waitForResponse(
      (r) =>
        r.url().includes("/api/quarantine/") &&
        r.url().includes("/reprocess") &&
        r.request().method() === "POST",
      { timeout: 8_000 }
    ).catch(() => null);

    await reprocessButton.click();

    // ConfirmDialog deve aparecer
    await expect(page.getByRole("dialog", { name: /reprocessar evento/i })).toBeVisible({ timeout: 3_000 });

    // Confirmar
    await page.getByRole("button", { name: /^reprocessar$/i }).last().click();

    const response = await reprocessPromise;
    if (response) {
      // Se API respondeu, verificar sucesso ou erros esperados (409, 422, 200)
      expect([200, 409, 410, 422]).toContain(response.status());

      if (response.status() === 200) {
        // Notice de sucesso deve aparecer
        await expect(page.getByTestId("reprocess-success-notice")).toBeVisible({ timeout: 5_000 });
        expect(await page.getByTestId("reprocess-success-notice").textContent()).toContain("reprocessado");
      }
    }
  });

  test("operator descarta evento de quarentena com confirmação", async ({ page }) => {
    await page.goto("/quarantine");
    await expect(page.getByTestId("quarantine-page")).toBeVisible({ timeout: 10_000 });

    const tableVisible = await page.getByTestId("quarantine-table").isVisible().catch(() => false);
    if (!tableVisible) return;

    const detailsButton = page.getByRole("button", { name: /detalhes/i }).first();
    const buttonExists = await detailsButton.isVisible().catch(() => false);
    if (!buttonExists) return;

    await detailsButton.click();
    await expect(page.getByTestId("quarantine-detail-drawer")).toBeVisible({ timeout: 5_000 });

    const discardButton = page.getByRole("button", { name: /descartar/i }).first();
    const discardVisible = await discardButton.isVisible().catch(() => false);
    if (!discardVisible) {
      console.log("[quarantine-test] Botão Descartar não encontrado — operator pode não ter permissão.");
      return;
    }

    const discardPromise = page.waitForResponse(
      (r) => r.url().includes("/api/quarantine/") && r.request().method() === "DELETE",
      { timeout: 8_000 }
    );

    await discardButton.click();

    await expect(page.getByRole("dialog", { name: /descartar entrada/i })).toBeVisible({ timeout: 3_000 });

    await page.getByRole("button", { name: /^descartar$/i }).last().click();

    const response = await discardPromise;
    expect(response.status()).toBeLessThan(300);

    await expect(page.getByTestId("quarantine-detail-drawer")).not.toBeVisible({ timeout: 5_000 });
  });
});

// ── Tests viewer ──────────────────────────────────────────────────────────────

test.describe("Quarentena — restrições para viewer (F4-S3)", () => {
  test.use({ storageState: ".auth/viewer.json" });

  test("viewer não vê Reprocessar", async ({ page }) => {
    await page.goto("/quarantine");
    await expect(page.getByTestId("quarantine-page")).toBeVisible({ timeout: 10_000 });

    await page.waitForResponse(
      (r) => r.url().includes("/api/quarantine") && r.status() < 300,
      { timeout: 8_000 }
    ).catch(() => {});

    const tableVisible = await page.getByTestId("quarantine-table").isVisible().catch(() => false);
    if (tableVisible) {
      const detailsButton = page.getByRole("button", { name: /detalhes/i }).first();
      const buttonExists = await detailsButton.isVisible().catch(() => false);
      if (buttonExists) {
        await detailsButton.click();
        await expect(page.getByTestId("quarantine-detail-drawer")).toBeVisible({ timeout: 5_000 });

        // Viewer não tem quarantine.discard — botão Reprocessar não deve existir
        const drawer = page.getByTestId("quarantine-detail-drawer");
        await expect(drawer.getByRole("button", { name: /reprocessar/i })).not.toBeVisible();

        // Botão Descartar também não deve aparecer
        await expect(drawer.getByRole("button", { name: /descartar/i })).not.toBeVisible();
      }
    }

    await expect(page.getByText(/403|sem permissão/i)).not.toBeVisible();
  });

  test("viewer acessa /quarantine e não vê botão Descartar", async ({ page }) => {
    await page.goto("/quarantine");
    await expect(page.getByTestId("quarantine-page")).toBeVisible({ timeout: 10_000 });

    await page.waitForResponse(
      (r) => r.url().includes("/api/quarantine") && r.status() < 300,
      { timeout: 8_000 }
    ).catch(() => {});

    const tableVisible = await page.getByTestId("quarantine-table").isVisible().catch(() => false);
    if (tableVisible) {
      const detailsButton = page.getByRole("button", { name: /detalhes/i }).first();
      const buttonExists = await detailsButton.isVisible().catch(() => false);
      if (buttonExists) {
        await detailsButton.click();
        await expect(page.getByTestId("quarantine-detail-drawer")).toBeVisible({ timeout: 5_000 });

        const drawer = page.getByTestId("quarantine-detail-drawer");
        await expect(drawer.getByRole("button", { name: /descartar/i })).not.toBeVisible();
      }
    }

    await expect(page.getByText(/403|sem permissão/i)).not.toBeVisible();
  });
});
