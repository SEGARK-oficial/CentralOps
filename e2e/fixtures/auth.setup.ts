/**
 * auth.setup.ts — Setup de autenticação para os 4 roles.
 *
 * Executado pelo projeto "setup" antes dos projetos de teste.
 * Salva storageState (cookies de sessão) em .auth/<role>.json
 * para ser reutilizado pelos workers Playwright sem refazer login.
 *
 * Roles cobertos:
 *   - admin     → .auth/admin.json
 *   - viewer    → .auth/viewer.json
 *   - operator  → .auth/operator.json
 *   - engineer  → .auth/engineer.json
 *
 * Credenciais correspondem ao que seed.ts cria.
 * IMPORTANTE: .auth/*.json está no .gitignore.
 */

import { test as setup } from "@playwright/test";
import path from "path";

// ── Credenciais E2E (devem espelhar seed.ts) ─────────────────────────────────

const CREDENTIALS = {
  admin:    { username: "admin",        password: "AdminPassword123!" },
  viewer:   { username: "viewer-e2e",   password: "Viewer123!" },
  operator: { username: "operator-e2e", password: "Operator123!" },
  engineer: { username: "engineer-e2e", password: "Engineer123!" },
} as const;

type Role = keyof typeof CREDENTIALS;

// ── Utilitário de login genérico ─────────────────────────────────────────────

/**
 * Realiza login como <role> e persiste o storageState em .auth/<role>.json.
 * Lida com o modo bootstrap (primeiro acesso) somente para admin.
 */
async function loginAs(
  page: import("@playwright/test").Page,
  role: Role,
): Promise<void> {
  const { username, password } = CREDENTIALS[role];

  await page.goto("/login");

  // Aguarda formulário carregar — evita preencher antes do AuthContext resolver
  await page.waitForSelector("form", { timeout: 10_000 });

  // Somente admin pode precisar do modo bootstrap
  if (role === "admin") {
    const setupRequired = await page
      .getByRole("heading", { name: /configurar acesso/i })
      .isVisible();

    if (setupRequired) {
      // Modo bootstrap — primeiro acesso ao ambiente E2E
      await page.getByLabel(/nome do administrador/i).fill("Admin E2E");
      await page.getByLabel(/usuário/i).first().fill(username);
      const senhaFields = await page.getByLabel(/senha/i).all();
      await senhaFields[0].fill(password);
      await senhaFields[1].fill(password);
      await page.getByRole("button", { name: /criar administrador/i }).click();
      await page.waitForURL(/\/(dashboard|$)/, { timeout: 15_000 });
      console.log("[auth.setup] Admin criado via bootstrap.");
      return;
    }
  }

  // Modo login normal — usuário já existe (criado pelo seed.ts)
  await page.getByLabel(/usuário/i).fill(username);
  await page.getByLabel(/senha/i).fill(password);
  await page.getByRole("button", { name: /entrar/i }).click();

  // Timeout generoso para CI com inicialização lenta
  await page.waitForURL(/\/(dashboard|$)/, { timeout: 15_000 });
}

// ── Setup tasks ───────────────────────────────────────────────────────────────

setup("autenticar admin", async ({ page }) => {
  await loginAs(page, "admin");
  await page.context().storageState({
    path: path.join(__dirname, "../.auth/admin.json"),
  });
  console.log("[auth.setup] Session admin salva em .auth/admin.json");
});

setup("autenticar viewer", async ({ page }) => {
  await loginAs(page, "viewer");
  await page.context().storageState({
    path: path.join(__dirname, "../.auth/viewer.json"),
  });
  console.log("[auth.setup] Session viewer salva em .auth/viewer.json");
});

setup("autenticar operator", async ({ page }) => {
  await loginAs(page, "operator");
  await page.context().storageState({
    path: path.join(__dirname, "../.auth/operator.json"),
  });
  console.log("[auth.setup] Session operator salva em .auth/operator.json");
});

setup("autenticar engineer", async ({ page }) => {
  await loginAs(page, "engineer");
  await page.context().storageState({
    path: path.join(__dirname, "../.auth/engineer.json"),
  });
  console.log("[auth.setup] Session engineer salva em .auth/engineer.json");
});
