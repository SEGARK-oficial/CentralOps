import { defineConfig, devices } from "@playwright/test";

/**
 * playwright.config.ts — Configuração principal do Playwright para CentralOps E2E.
 *
 * Projetos:
 *   - setup      : autentica 4 roles, salva .auth/*.json
 *   - admin      : testes gerais com sessão admin (01/02 POC + fluxos admin)
 *   - engineer   : testes que precisam de permissão mapping.write / mapping.rollback
 *   - operator   : testes de drift.ignore / quarantine.discard
 *   - viewer     : testes de restrição de acesso (o que NÃO pode ser feito)
 *   - rbac       : testes parametrizados de matriz (role-*.spec.ts)
 *
 * Cada arquivo de teste que precisa de role específico usa:
 *   test.use({ storageState: ".auth/<role>.json" })
 *
 * Testes sem storageState explícito herdam o projeto admin (compatibilidade POC).
 */
export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 4 : 2,
  reporter: [
    ["html", { open: "never" }],
    ["github"],
    // Formato de lista para debug local rápido
    ...(process.env.CI ? [] : [["list"] as ["list"]]),
  ],
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:3000",
    // The app now auto-detects the browser locale (react-i18next). The E2E specs
    // assert against the Portuguese UI, so pin the browser locale to pt-BR — the
    // language detector maps it to `pt` and the app renders Portuguese
    // deterministically (otherwise CI's en-US browser renders English and every
    // PT selector — getByLabel(/usuário/i), name:/entrar/i — times out).
    locale: "pt-BR",
    trace: "on-first-retry",
    video: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    // ── Projeto de setup global — deve rodar antes de tudo ────────────────
    {
      name: "setup",
      // O arquivo de autenticação (auth.setup.ts) vive em e2e/fixtures/, FORA do
      // testDir global "./tests". Sem um testDir próprio aqui, o Playwright não
      // o descobre e o projeto falha com "No tests found". Os demais projetos
      // continuam herdando "./tests".
      testDir: "./fixtures",
      testMatch: /.*\.setup\.ts/,
    },

    // ── Projeto admin — testes POC + fluxos de admin ──────────────────────
    {
      name: "admin",
      use: {
        ...devices["Desktop Chrome"],
        storageState: ".auth/admin.json",
      },
      dependencies: ["setup"],
      // Exclui arquivos de role específico — cada um define seu próprio storageState
      testIgnore: /\/(03|04|05|06|07|08)-.*\.spec\.ts/,
    },

    // ── Projetos de role específico (Sprint 2-5) ──────────────────────────
    // Cada spec desses arquivos chama test.use({ storageState }) internamente.
    // Os projetos abaixo existem para que o sharding distribua corretamente.
    {
      name: "sprint2-mapping-edit",
      use: {
        ...devices["Desktop Chrome"],
        // storageState sobrescrito em cada describe block dentro do spec
        storageState: ".auth/engineer.json",
      },
      dependencies: ["setup"],
      testMatch: /03-mapping-edit-save-rollback\.spec\.ts/,
    },
    {
      name: "sprint3-drift",
      use: {
        ...devices["Desktop Chrome"],
        storageState: ".auth/operator.json",
      },
      dependencies: ["setup"],
      testMatch: /04-drift-explorer\.spec\.ts/,
    },
    {
      name: "sprint3-quarantine",
      use: {
        ...devices["Desktop Chrome"],
        storageState: ".auth/operator.json",
      },
      dependencies: ["setup"],
      testMatch: /05-quarantine\.spec\.ts/,
    },
    {
      name: "sprint4-rbac-admin",
      use: {
        ...devices["Desktop Chrome"],
        storageState: ".auth/admin.json",
      },
      dependencies: ["setup"],
      testMatch: /06-rbac-admin-users\.spec\.ts/,
    },
    {
      name: "sprint5-health",
      use: {
        ...devices["Desktop Chrome"],
        storageState: ".auth/admin.json",
      },
      dependencies: ["setup"],
      testMatch: /07-pipeline-health\.spec\.ts/,
    },
    {
      name: "sprint4-rbac-matrix",
      use: {
        ...devices["Desktop Chrome"],
        // Storagestate por describe block — spec altera via test.use()
        storageState: ".auth/viewer.json",
      },
      dependencies: ["setup"],
      testMatch: /08-rbac-matrix\.spec\.ts/,
    },
  ],
  webServer: process.env.CI
    ? undefined // CI sobe via docker-compose.e2e.yml no workflow
    : {
        command:
          "echo 'Use docker compose -f compose/docker-compose.e2e.yml up para rodar local'",
        url: "http://localhost:3000",
        reuseExistingServer: true,
        timeout: 120_000,
      },
});
