# CentralOps — E2E Test Suite (Playwright)

Fase 3, Sprint 6. Testes E2E com [Playwright](https://playwright.dev/) 1.44+ cobrindo Sprints 1-5.

## Pré-requisitos

- Node.js 20+
- Docker + Docker Compose
- Chromium: `cd e2e && npx playwright install chromium --with-deps`

## Como rodar localmente

### 1. Subir o stack E2E

```bash
# A partir da raiz do repo
docker compose -f compose/docker-compose.e2e.yml up --build -d

# Aguardar o healthcheck do backend (até 2 minutos)
docker compose -f compose/docker-compose.e2e.yml ps
```

### 2. Popular o banco e o Redis

```bash
cd e2e
npx ts-node seed.ts

# Redis (eventos sintéticos para dry-run)
cd ..
bash scripts/seed-redis-e2e.sh
```

### 3. Executar os testes

```bash
cd e2e

# Todos os testes (todos os projetos)
npx playwright test

# Projeto específico por role
npx playwright test --project=sprint2-mapping-edit
npx playwright test --project=sprint3-drift
npx playwright test --project=sprint3-quarantine
npx playwright test --project=sprint4-rbac-admin
npx playwright test --project=sprint5-health
npx playwright test --project=sprint4-rbac-matrix

# Arquivo específico
npx playwright test tests/03-mapping-edit-save-rollback.spec.ts

# Com browser visível (debug)
npx playwright test --headed tests/04-drift-explorer.spec.ts

# Listar todos os cenários sem rodar
npx playwright test --list
```

### 4. Ver relatório HTML

```bash
npx playwright show-report
```

### 5. Derrubar stack

```bash
docker compose -f compose/docker-compose.e2e.yml down -v
```

## Como adicionar nova fixture de role

1. Adicionar as credenciais em `e2e/seed.ts` na função `createUser`.
2. Adicionar o setup task em `e2e/fixtures/auth.setup.ts`:

```typescript
setup("autenticar <role>", async ({ page }) => {
  await loginAs(page, "<role>");
  await page.context().storageState({
    path: path.join(__dirname, "../.auth/<role>.json"),
  });
});
```

3. Adicionar o projeto em `playwright.config.ts`:

```typescript
{
  name: "sprint-N-<feature>",
  use: {
    ...devices["Desktop Chrome"],
    storageState: ".auth/<role>.json",
  },
  dependencies: ["setup"],
  testMatch: /NN-<feature>\.spec\.ts/,
},
```

4. Nos testes que precisam de role específico, usar `test.use({ storageState: ".auth/<role>.json" })` no topo do describe block.

## Convenção de selectors

Ordem de preferência:

1. `getByRole` com `name` — mais próximo de como o usuário percebe a UI
2. `getByLabel` — para campos de formulário
3. `getByText` — para conteúdo estático visível
4. `getByTestId` — fallback quando role/aria não são suficientes

Nunca usar:
- Seletores por classe CSS — mudam com refatoração
- XPath — frágil e ilegível
- Índices numéricos como único seletor (`.nth(3)`)

Convenção de `data-testid`:

```
data-testid="<componente>--<elemento>"
# Exemplos:
data-testid="mapping-editor-page"
data-testid="edit-mode-button"
data-testid="save-modal"
data-testid="commit-message-input"
data-testid="confirm-save"
data-testid="versions-tab"
data-testid="audit-tab"
data-testid="drift-explorer-page"
data-testid="quarantine-page"
data-testid="quarantine-detail-drawer"
data-testid="integration-health-panel"
data-testid="health-refresh-button"
data-testid="admin-users-page"
data-testid="users-table"
data-testid="new-user-button"
```

## Política de retry e flakiness

- **Retries CI**: 2 (configurado em `playwright.config.ts`)
- **Retries local**: 0 — se falhar local, é bug real
- **Flakiness threshold**: qualquer teste com taxa de falha > 5% nas últimas 10 runs
  deve ser corrigido imediatamente ou marcado com `test.skip` com issue linkada
- **Nunca usar `waitForTimeout`**: sempre `waitForResponse`, `waitForURL` ou `toBeVisible`

## Cenários por sprint

### Sprint 0 (infra)
- [x] Scaffold: playwright.config.ts, docker-compose.e2e.yml, seed.ts
- [x] POC 01-mapping-editor-read: 3 painéis visíveis
- [x] POC 02-dry-run-live: dry-run popula envelope

### Sprint 2 (Mapping Editor edit mode)
- [x] `03-mapping-edit-save-rollback` — engineer entra em edit mode
- [x] `03-mapping-edit-save-rollback` — save bloqueado sem commit message
- [x] `03-mapping-edit-save-rollback` — save bloqueado com commit < 10 chars
- [x] `03-mapping-edit-save-rollback` — salva nova versão com commit válido
- [x] `03-mapping-edit-save-rollback` — aba Versões mostra histórico
- [x] `03-mapping-edit-save-rollback` — rollback via aba Versões
- [x] `03-mapping-edit-save-rollback` — aba Auditoria disponível pós-rollback
- [x] `03-mapping-edit-save-rollback` — viewer não vê botão Editar regras
- [x] `03-mapping-edit-save-rollback` — viewer acessa editor em modo leitura

### Sprint 3 (Drift + Quarantine)
- [x] `04-drift-explorer` — página /drift carrega
- [x] `04-drift-explorer` — operator filtra por status 'novo'
- [x] `04-drift-explorer` — operator ignora campo com confirmação
- [x] `04-drift-explorer` — engineer cria regra a partir de drift entry
- [x] `04-drift-explorer` — viewer não vê Ignorar nem Remover
- [x] `04-drift-explorer` — viewer acessa /drift sem 403
- [x] `04-drift-explorer` — filtros combinados não geram 500
- [x] `05-quarantine` — página /quarantine carrega sem erro
- [x] `05-quarantine` — operator abre drawer com payload bruto
- [x] `05-quarantine` — Reprocessar está disabled com explicação
- [x] `05-quarantine` — operator descarta com confirmação
- [x] `05-quarantine` — viewer não vê Descartar no drawer

### Sprint 4 (RBAC)
- [x] `06-rbac-admin-users` — admin vê tabela com 4 usuários
- [x] `06-rbac-admin-users` — admin muda role via modal
- [x] `06-rbac-admin-users` — admin vê matriz de permissões
- [x] `06-rbac-admin-users` — viewer bloqueado em /admin/users
- [x] `06-rbac-admin-users` — operator não vê 'Novo usuário'
- [x] `08-rbac-matrix` — viewer: 4 rotas de leitura OK
- [x] `08-rbac-matrix` — viewer: mapping.write negado
- [x] `08-rbac-matrix` — viewer: drift.ignore negado
- [x] `08-rbac-matrix` — viewer: quarantine.discard negado
- [x] `08-rbac-matrix` — viewer: user.manage negado
- [x] `08-rbac-matrix` — operator: mapping.write negado
- [x] `08-rbac-matrix` — operator: drift.ignore permitido
- [x] `08-rbac-matrix` — operator: user.manage negado
- [x] `08-rbac-matrix` — engineer: mapping.write permitido
- [x] `08-rbac-matrix` — engineer: mapping.rollback permitido
- [x] `08-rbac-matrix` — engineer: user.manage negado
- [x] `08-rbac-matrix` — admin: user.manage permitido
- [x] `08-rbac-matrix` — admin: mapping.write permitido
- [x] `08-rbac-matrix` — admin: acesso a 5 rotas críticas

### Sprint 5 (Pipeline Health)
- [x] `07-pipeline-health` — aba Saúde de Normalização renderiza painel
- [x] `07-pipeline-health` — botão Atualizar dispara nova requisição
- [x] `07-pipeline-health` — página /health carrega e lista cards
- [x] `07-pipeline-health` — filtro 'Saudáveis' sem erro
- [x] `07-pipeline-health` — filtro 'Com problema' sem erro
- [x] `07-pipeline-health` — viewer acessa /health sem 403

## Diagnóstico de falhas

Artefatos de falha ficam em `playwright-report/` (ignorado pelo git).
No CI são publicados como artifacts por 7 dias.

```bash
# Ver último relatório
npx playwright show-report

# Modo trace (debug visual passo a passo)
npx playwright test --trace on tests/03-*.spec.ts
npx playwright show-trace test-results/*/trace.zip

# Rodar apenas testes com falha
npx playwright test --last-failed
```
