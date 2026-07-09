/**
 * 06-rbac-admin-users.spec.ts — Administração de usuários: RBAC Sprint 4.
 *
 * Cobre:
 *   - admin lista usuários e muda role
 *   - não-admin é barrado ao acessar /admin/users
 *   - RolePermissionsViewer mostra matriz de permissões
 *
 * O que este arquivo NÃO valida:
 *   - Criação de usuário (fluxo de NewUserModal — coberto no Vitest)
 *   - Edição de dados do usuário (EditUserModal — coberto no Vitest)
 *   - Desativação/exclusão (cobertos no Vitest de AdminUsersPage)
 *   - Persistência além do reload (backend pytest)
 */

import { test, expect } from "@playwright/test";

// ── Tests admin ───────────────────────────────────────────────────────────────

test.describe("Admin Users — ações como admin (Sprint 4)", () => {
  test.use({ storageState: ".auth/admin.json" });

  test("admin acessa /admin/users e vê tabela de usuários", async ({ page }) => {
    await page.goto("/admin/users");
    await expect(page.getByTestId("admin-users-page")).toBeVisible({ timeout: 10_000 });

    // Tabela de usuários deve aparecer — data-testid="users-table"
    await expect(page.getByTestId("users-table")).toBeVisible({ timeout: 8_000 });

    // Seed criou 4 usuários: admin, viewer-e2e, operator-e2e, engineer-e2e.
    // A tabela exibe o username como @username — usamos "@admin" (texto "admin"
    // sozinho colide com a role/dropdown e dá strict-mode).
    await expect(page.getByText("@admin")).toBeVisible();
  });

  test("admin vê os 4 usuários seedados na tabela", async ({ page }) => {
    await page.goto("/admin/users");
    await expect(page.getByTestId("users-table")).toBeVisible({ timeout: 10_000 });

    // Todos os usuários criados pelo seed devem estar na tabela
    // Usamos texto do username exibido como @username
    await expect(page.getByText("@viewer-e2e")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText("@operator-e2e")).toBeVisible({ timeout: 5_000 });
    await expect(page.getByText("@engineer-e2e")).toBeVisible({ timeout: 5_000 });
  });

  test("admin muda role de usuário via modal de edição de papel", async ({ page, request }) => {
    // Mutamos um usuário DESCARTÁVEL (criado/deletado via API) em vez de viewer-e2e:
    // as sessões dos 4 roles (.auth/*.json) são usadas por testes de RBAC que rodam
    // EM PARALELO, e cujas permissões dependem do papel ATUAL do usuário — mudar o
    // papel de um deles os tornaria flaky.
    const username = `rbac-mut-${Date.now()}`;
    const createRes = await request.post("/api/auth/users", {
      data: { username, display_name: "RBAC Mutável", password: "RbacMut123!", role: "viewer" },
    });
    expect(createRes.ok()).toBeTruthy();
    const created = (await createRes.json()) as { id: string };

    try {
      await page.goto("/admin/users");
      await expect(page.getByTestId("users-table")).toBeVisible({ timeout: 10_000 });

      const row = page.getByRole("row").filter({ hasText: `@${username}` });
      await row.getByRole("button", { name: /editar papel/i }).click();
      await expect(page.getByRole("dialog")).toBeVisible({ timeout: 5_000 });

      // O modal usa um <select> NATIVO (data-testid="role-select"). O usuário nasceu
      // como "viewer" → mudamos para "operator" (o botão Salvar só habilita quando
      // há mudança de papel: disabled={user.role === selectedRole}).
      await page.getByTestId("role-select").selectOption("operator");

      // A atualização de papel usa PUT /api/auth/users/<id> (não PATCH).
      const updatePromise = page.waitForResponse(
        (r) => r.url().includes("/api/auth/users/") && r.request().method() === "PUT",
        { timeout: 8_000 }
      );
      await page.getByRole("button", { name: /salvar|confirmar/i }).last().click();

      const response = await updatePromise;
      expect(response.status()).toBeLessThan(300);

      // Modal fecha ao concluir (o status <300 acima já prova o sucesso).
      await expect(page.getByRole("dialog")).not.toBeVisible({ timeout: 5_000 });
    } finally {
      // Limpa o usuário descartável (não polui /admin/users entre runs).
      await request.delete(`/api/auth/users/${created.id}`).catch(() => {});
    }
  });

  test("admin abre RolePermissionsViewer e vê matriz de 4 papéis", async ({ page }) => {
    await page.goto("/admin/users");
    await expect(page.getByTestId("admin-users-page")).toBeVisible({ timeout: 10_000 });

    // Clicar no botão "Ver permissões"
    await page.getByRole("button", { name: /ver permissões/i }).click();

    // Modal deve abrir com a tabela de matriz
    await expect(page.getByRole("dialog", { name: /matriz de permissões/i })).toBeVisible({ timeout: 5_000 });

    // A tabela deve ter colunas para os 4 papéis
    const dialog = page.getByRole("dialog", { name: /matriz de permissões/i });
    // A matriz tem muitas células contendo cada papel (cabeçalho + uma por
    // permissão, via aria-label "<papel> tem <perm>"), então usamos .first()
    // para evitar strict-mode — basta confirmar que o papel aparece na matriz.
    await expect(dialog.getByRole("cell", { name: /viewer/i }).first()).toBeVisible({ timeout: 5_000 });
    await expect(dialog.getByRole("cell", { name: /operator/i }).first()).toBeVisible();
    await expect(dialog.getByRole("cell", { name: /engineer/i }).first()).toBeVisible();
    await expect(dialog.getByRole("cell", { name: /admin/i }).first()).toBeVisible();

    // Fechar o modal
    await page.getByRole("button", { name: /fechar/i }).click();
    await expect(page.getByRole("dialog")).not.toBeVisible({ timeout: 3_000 });
  });
});

// ── Tests não-admin ───────────────────────────────────────────────────────────

test.describe("Admin Users — restrições para viewer (Sprint 4)", () => {
  test.use({ storageState: ".auth/viewer.json" });

  test("viewer é redirecionado ou bloqueado ao acessar /admin/users", async ({ page }) => {
    await page.goto("/admin/users");

    // Aguarda estabilizar — pode redirecionar para /dashboard ou mostrar 403
    await page.waitForLoadState("networkidle", { timeout: 8_000 }).catch(() => {});

    const currentUrl = page.url();
    const isOnAdminPage = currentUrl.includes("/admin/users");

    if (isOnAdminPage) {
      // Se chegou na página, deve mostrar mensagem de acesso negado
      // ou a tabela de usuários NÃO deve estar visível
      const tableVisible = await page.getByTestId("users-table").isVisible().catch(() => false);
      expect(tableVisible).toBe(false);
    } else {
      // Foi redirecionado — comportamento esperado para non-admin
      expect(currentUrl).not.toContain("/admin/users");
    }
  });
});

test.describe("Admin Users — restrições para operator (Sprint 4)", () => {
  test.use({ storageState: ".auth/operator.json" });

  test("operator não vê botão 'Novo usuário' em /admin/users", async ({ page }) => {
    await page.goto("/admin/users");

    // Operator pode ou não ter acesso à página, mas não deve ter user.manage
    await page.waitForLoadState("networkidle", { timeout: 8_000 }).catch(() => {});

    // Se a página carregou, botão de criação não deve aparecer
    await expect(page.getByTestId("new-user-button")).not.toBeVisible();
  });
});
