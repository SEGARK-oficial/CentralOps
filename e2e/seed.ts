/**
 * seed.ts — Popular banco de dados E2E via API após app subir.
 *
 * Executado UMA vez antes da suíte Playwright:
 *   npx ts-node e2e/seed.ts
 *
 * Estratégia de IDs: API retorna IDs reais; seed é idempotente via 409.
 *
 * Endpoints consumidos:
 *   GET  /api/auth/status           → verificar backend healthy
 *   POST /api/auth/bootstrap         → criar admin inicial
 *   POST /api/auth/login             → obter session cookie
 *   POST /api/auth/users             → criar outros roles
 *   POST /api/organizations          → criar org de teste
 *   GET  /api/mappings               → confirmar defaults + capturar ID
 *   POST /api/drift/seed-e2e         → popular unknown_fields (endpoint admin)
 *   POST /api/quarantine/seed-e2e    → popular quarantine_events (endpoint admin)
 *   POST /api/integrations           → criar integrações de teste
 *
 * Após seed:
 *   - 1 admin (bootstrap)
 *   - viewer-e2e / operator-e2e / engineer-e2e
 *   - 1 organização "E2E Org"
 *   - 2 integrações (e2e-healthy, e2e-unhealthy)
 *   - 5 drift entries (vendor sophos)
 *   - 5 quarantine entries
 *   - Mappings default via migration backend
 *
 * NOTA: endpoints /seed-e2e são gates por flag E2E_SEED_ENABLED=true no backend.
 * Se não existirem (404), o seed loga aviso e continua — testes de drift/quarantine
 * vão reportar lista vazia ao invés de falhar no setup.
 */

const BASE_URL = process.env.BASE_URL ?? "http://localhost:3000";
const ADMIN_USERNAME = "admin";
const ADMIN_PASSWORD = "AdminPassword123!";
const MAX_RETRIES = 30;
const RETRY_INTERVAL_MS = 2000;

// ── Utilitários ──────────────────────────────────────────────────────────────

async function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Aguarda backend retornar 200 em GET /api/auth/status.
 */
async function waitForBackend(): Promise<void> {
  console.log(`[seed] Aguardando backend em ${BASE_URL}/api/auth/status ...`);
  for (let i = 0; i < MAX_RETRIES; i++) {
    try {
      const res = await fetch(`${BASE_URL}/api/auth/status`);
      if (res.ok) {
        const data = (await res.json()) as { setup_required: boolean };
        console.log(`[seed] Backend disponível. setup_required=${data.setup_required}`);
        return;
      }
    } catch {
      // Conexão recusada — backend ainda está subindo
    }
    console.log(`[seed] Tentativa ${i + 1}/${MAX_RETRIES} falhou, aguardando ${RETRY_INTERVAL_MS}ms...`);
    await sleep(RETRY_INTERVAL_MS);
  }
  throw new Error(`[seed] Backend não ficou disponível após ${MAX_RETRIES} tentativas.`);
}

/**
 * POST /api/auth/bootstrap — cria admin inicial.
 * Se admin já existe (409), realiza login normal.
 */
async function bootstrapAdmin(): Promise<string> {
  console.log("[seed] Criando admin via bootstrap...");
  const res = await fetch(`${BASE_URL}/api/auth/bootstrap`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: ADMIN_USERNAME,
      display_name: "Admin E2E",
      password: ADMIN_PASSWORD,
    }),
  });

  if (res.status === 409) {
    console.log("[seed] Admin já existe (409) — fazendo login...");
    return await loginAdmin();
  }

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`[seed] bootstrap falhou: ${res.status} ${body}`);
  }

  const setCookie = res.headers.get("set-cookie") ?? "";
  console.log("[seed] Admin criado com sucesso.");
  return setCookie;
}

/**
 * POST /api/auth/login — autentica admin.
 */
async function loginAdmin(): Promise<string> {
  const res = await fetch(`${BASE_URL}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: ADMIN_USERNAME, password: ADMIN_PASSWORD }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`[seed] login falhou: ${res.status} ${body}`);
  }

  const setCookie = res.headers.get("set-cookie") ?? "";
  console.log("[seed] Login admin realizado.");
  return setCookie;
}

/**
 * POST /api/auth/users — cria usuário com role específico.
 * Senhas correspondem ao que auth.setup.ts usa.
 * Idempotente: 409 é ignorado.
 */
async function createUser(
  sessionCookie: string,
  opts: { username: string; display_name: string; password: string; role: string }
): Promise<void> {
  const res = await fetch(`${BASE_URL}/api/auth/users`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: sessionCookie,
    },
    body: JSON.stringify(opts),
  });

  if (res.status === 409) {
    console.log(`[seed] Usuário '${opts.username}' já existe — pulando.`);
    return;
  }

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`[seed] Falha ao criar usuário '${opts.username}': ${res.status} ${body}`);
  }

  console.log(`[seed] Usuário '${opts.username}' (${opts.role}) criado.`);
}

/**
 * POST /api/organizations — cria org de teste.
 * Verifica lista antes para idempotência.
 * Retorna o ID da organização criada/existente.
 */
async function createOrganization(sessionCookie: string): Promise<number | null> {
  const listRes = await fetch(`${BASE_URL}/api/organizations`, {
    headers: { Cookie: sessionCookie },
  });

  if (listRes.ok) {
    const orgs = (await listRes.json()) as Array<{ id: number; name: string }>;
    const existing = orgs.find((o) => o.name === "E2E Org");
    if (existing) {
      console.log(`[seed] Organização 'E2E Org' já existe (id=${existing.id}) — pulando.`);
      return existing.id;
    }
  }

  const res = await fetch(`${BASE_URL}/api/organizations`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: sessionCookie,
    },
    body: JSON.stringify({
      name: "E2E Org",
      description: "Organização criada automaticamente para testes E2E",
      is_active: true,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`[seed] Falha ao criar organização: ${res.status} ${body}`);
  }

  const org = (await res.json()) as { id: number };
  console.log(`[seed] Organização 'E2E Org' criada (id=${org.id}).`);
  return org.id;
}

/**
 * POST /api/integrations — cria integração de teste.
 * Retorna o ID criado ou null se já existe (verifica por nome).
 */
async function createIntegration(
  sessionCookie: string,
  orgId: number,
  name: string,
  platform: "sophos" | "wazuh" = "sophos",
): Promise<number | null> {
  // Verificar se já existe
  const listRes = await fetch(`${BASE_URL}/api/integrations`, {
    headers: { Cookie: sessionCookie },
  });
  if (listRes.ok) {
    const integrations = (await listRes.json()) as Array<{ id: number; name: string }>;
    const existing = integrations.find((i) => i.name === name);
    if (existing) {
      console.log(`[seed] Integração '${name}' já existe (id=${existing.id}) — pulando.`);
      return existing.id;
    }
  }

  // Credenciais fake: o ambiente E2E não tem Sophos/Wazuh reais. Esses campos
  // são obrigatórios na validação do backend (POST /api/integrations). Para
  // Sophos passamos `region` para PULAR o test_connection automático (que
  // falharia/demoraria com credencial fake) — a integração é criada mesmo assim.
  const platformFields =
    platform === "sophos"
      ? {
          client_id: "e2e-fake-client-id",
          client_secret: "e2e-fake-client-secret",
          region: "us03",
        }
      : {
          manager_url: "https://wazuh-e2e.invalid:55000",
          manager_api_username: "e2e-wazuh-user",
          manager_api_password: "e2e-wazuh-pass",
        };

  const res = await fetch(`${BASE_URL}/api/integrations`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: sessionCookie,
    },
    body: JSON.stringify({
      organization_id: orgId,
      name,
      platform,
      is_active: true,
      ...platformFields,
    }),
  });

  if (!res.ok) {
    const body = await res.text();
    console.warn(`[seed] Falha ao criar integração '${name}': ${res.status} ${body}`);
    return null;
  }

  const integration = (await res.json()) as { id: number };
  console.log(`[seed] Integração '${name}' criada (id=${integration.id}).`);
  return integration.id;
}

/**
 * Verifica que mappings default foram seedados pela migration.
 * Retorna o ID do primeiro mapping encontrado (usado em testes de edição).
 */
async function verifyMappings(sessionCookie: string): Promise<string | null> {
  const res = await fetch(`${BASE_URL}/api/mappings`, {
    headers: { Cookie: sessionCookie },
  });

  if (!res.ok) {
    console.warn(`[seed] GET /api/mappings retornou ${res.status} — verifique a migration.`);
    return null;
  }

  const mappings = (await res.json()) as Array<{ id: string; vendor: string; event_type: string }>;
  console.log(`[seed] Mappings disponíveis: ${mappings.length} entries.`);

  if (mappings.length === 0) {
    console.warn("[seed] AVISO: nenhum mapping encontrado. Migration pode não ter rodado.");
    return null;
  }

  // Logar o ID do primeiro para facilitar debug de testes
  console.log(`[seed] Primeiro mapping: id=${mappings[0].id} vendor=${mappings[0].vendor}`);
  return mappings[0].id;
}

/**
 * Popula drift entries via endpoint admin de seed.
 * Este endpoint só existe quando E2E_SEED_ENABLED=true no backend.
 * Se não existir, loga aviso e continua graciosamente.
 */
async function seedDriftEntries(sessionCookie: string): Promise<void> {
  const payload = {
    entries: [
      { vendor: "sophos", event_type: "sophos.alert", field_path: "data.threatName", sample_value: "Trojan.Gen.2", occurrence_count: 42 },
      { vendor: "sophos", event_type: "sophos.alert", field_path: "data.cleanupResult", sample_value: "cleaned", occurrence_count: 31 },
      { vendor: "sophos", event_type: "sophos.alert", field_path: "data.originPath", sample_value: "/tmp/malware.exe", occurrence_count: 7 },
      { vendor: "sophos", event_type: "sophos.event", field_path: "endpoint.name", sample_value: "DESKTOP-ABC123", occurrence_count: 15 },
      { vendor: "sophos", event_type: "sophos.event", field_path: "endpoint.group", sample_value: "Servers", occurrence_count: 9 },
      { vendor: "wazuh", event_type: "wazuh.alert", field_path: "data.srcip", sample_value: "192.168.1.1", occurrence_count: 100 },
    ],
  };

  const res = await fetch(`${BASE_URL}/api/drift/seed-e2e`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: sessionCookie,
    },
    body: JSON.stringify(payload),
  });

  // 404 = endpoint não montado; 405 = path colide com DELETE /{field_id} porque
  // o endpoint admin seed-e2e não está implementado. Em ambos os casos tratamos
  // como ausente e seguimos (drift fica vazio — não bloqueia o seed).
  if (res.status === 404 || res.status === 405) {
    console.warn(`[seed] AVISO: endpoint /api/drift/seed-e2e indisponível (${res.status}). Testes de drift terão lista vazia.`);
    return;
  }

  if (!res.ok) {
    const body = await res.text();
    console.warn(`[seed] AVISO: seed de drift falhou: ${res.status} ${body}`);
    return;
  }

  console.log(`[seed] Drift entries seedados: ${payload.entries.length} entries.`);
}

/**
 * Popula quarantine events via endpoint admin de seed.
 * Idempotente — endpoint deve verificar duplicatas por vendor+event_type+error_kind.
 */
async function seedQuarantineEvents(
  sessionCookie: string,
  integrationId: number | null,
): Promise<void> {
  const entries = [
    {
      vendor: "sophos",
      event_type: "sophos.alert",
      error_kind: "schema_error",
      error_detail: "Campo obrigatório 'severity' ausente no payload",
      raw_payload: { id: "evt-001", type: "Alert", data: { threatId: "T123" } },
      integration_id: integrationId,
    },
    {
      vendor: "sophos",
      event_type: "sophos.event",
      error_kind: "type_cast_failed",
      error_detail: "Não foi possível converter 'timestamp' para epoch",
      raw_payload: { id: "evt-002", type: "Event", timestamp: "invalid-date" },
      integration_id: integrationId,
    },
    {
      vendor: "wazuh",
      event_type: "wazuh.alert",
      error_kind: "missing_required",
      error_detail: "rule.id obrigatório não encontrado",
      raw_payload: { id: "wz-001", agent: { name: "server01" } },
      integration_id: integrationId,
    },
    {
      vendor: "sophos",
      event_type: "sophos.alert",
      error_kind: "value_map_no_match",
      error_detail: "Valor 'UNKNOWN_SEVERITY' não encontrado no mapa",
      raw_payload: { id: "evt-003", severity: "UNKNOWN_SEVERITY" },
      integration_id: integrationId,
    },
    {
      vendor: "wazuh",
      event_type: "wazuh.event",
      error_kind: "jmespath_eval_failed",
      error_detail: "Expressão JMESPath inválida: 'data.[invalid'",
      raw_payload: { id: "wz-002" },
      integration_id: integrationId,
    },
  ];

  const res = await fetch(`${BASE_URL}/api/quarantine/seed-e2e`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: sessionCookie,
    },
    body: JSON.stringify({ entries }),
  });

  if (res.status === 404 || res.status === 405) {
    console.warn(`[seed] AVISO: endpoint /api/quarantine/seed-e2e indisponível (${res.status}). Testes de quarantine terão lista vazia.`);
    return;
  }

  if (!res.ok) {
    const body = await res.text();
    console.warn(`[seed] AVISO: seed de quarantine falhou: ${res.status} ${body}`);
    return;
  }

  console.log(`[seed] Quarantine events seedados: ${entries.length} entries.`);
}

/**
 * Cria versão de mapping via API para garantir que há pelo menos v2
 * (necessário para testar rollback e auditoria).
 */
async function seedMappingVersion(
  sessionCookie: string,
  mappingId: string,
): Promise<void> {
  // Buscar versão atual primeiro
  const res = await fetch(`${BASE_URL}/api/mappings/${mappingId}`, {
    headers: { Cookie: sessionCookie },
  });

  if (!res.ok) {
    console.warn(`[seed] Não foi possível buscar mapping ${mappingId}: ${res.status}`);
    return;
  }

  const mapping = (await res.json()) as {
    versions?: Array<{ version_number: number; rules: { preprocess?: unknown[]; rules?: unknown[] } }>;
  };
  const versions = mapping.versions ?? [];

  // Se já há 2+ versões, não criar mais (idempotência)
  if (versions.length >= 2) {
    console.log(`[seed] Mapping ${mappingId} já tem ${versions.length} versões — pulando criação de v2.`);
    return;
  }

  // DSL v2: `rules` é um objeto { preprocess, rules } — NÃO uma lista. A lista
  // de regras fica em `.rules`. Preserva preprocess + regras da versão atual e
  // anexa uma regra de teste, mantendo o shape v2 que POST /versions exige
  // (CreateVersionRequest.rules é Dict[str, Any] com preprocess + rules).
  const baseV2 = versions[0]?.rules ?? {};
  const basePreprocess = Array.isArray(baseV2.preprocess) ? baseV2.preprocess : [];
  const baseRules = Array.isArray(baseV2.rules) ? baseV2.rules : [];
  const v2Rules = {
    preprocess: basePreprocess,
    rules: [
      ...baseRules,
      {
        target: "metadata.e2e_seed",
        const: "v2-seeded",
        required: false,
      },
    ],
  };

  const createRes = await fetch(`${BASE_URL}/api/mappings/${mappingId}/versions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Cookie: sessionCookie,
    },
    body: JSON.stringify({
      rules: v2Rules,
      commit_message: "Seed E2E: versão v2 para testes de rollback",
    }),
  });

  if (!createRes.ok) {
    const body = await createRes.text();
    console.warn(`[seed] Falha ao criar v2 do mapping ${mappingId}: ${createRes.status} ${body}`);
    return;
  }

  console.log(`[seed] Versão v2 criada para mapping ${mappingId}.`);
}

// ── Entrypoint ───────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  console.log("[seed] Iniciando seed E2E (Sprint 6)...");

  await waitForBackend();

  const sessionCookie = await bootstrapAdmin();

  // Criar usuários dos 3 roles — senhas correspondem ao auth.setup.ts
  await createUser(sessionCookie, {
    username: "viewer-e2e",
    display_name: "Viewer E2E",
    password: "Viewer123!",
    role: "viewer",
  });

  await createUser(sessionCookie, {
    username: "operator-e2e",
    display_name: "Operator E2E",
    password: "Operator123!",
    role: "operator",
  });

  await createUser(sessionCookie, {
    username: "engineer-e2e",
    display_name: "Engineer E2E",
    password: "Engineer123!",
    role: "engineer",
  });

  // Criar organização e integrações
  const orgId = await createOrganization(sessionCookie);

  if (orgId !== null) {
    // Integração "saudável" — last_success_at recente (definido pelo backend ao criar)
    await createIntegration(sessionCookie, orgId, "e2e-integration-healthy", "sophos");
    // Integração "não saudável" — sem dados de coleta (last_success_at null)
    await createIntegration(sessionCookie, orgId, "e2e-integration-unhealthy", "wazuh");

    // o reservoir é por-org. Persiste o orgId para (1) o
    // seed-redis-e2e.sh popular sob a chave certa e (2) o spec 02 setar o filtro
    // de org do admin (selectedOrgId) — senão o dry-run do admin global lê vazio.
    const fs = await import("node:fs");
    // cwd-relative (o seed roda sempre com cwd=e2e/): funciona em CJS e ESM.
    fs.writeFileSync(".e2e-org-id", String(orgId));
    console.log(`[seed] orgId=${orgId} persistido em e2e/.e2e-org-id`);
  }

  // Verificar mappings e criar versão adicional para testes de rollback
  const firstMappingId = await verifyMappings(sessionCookie);
  if (firstMappingId) {
    await seedMappingVersion(sessionCookie, firstMappingId);
  }

  // Popular dados de drift e quarantine
  await seedDriftEntries(sessionCookie);
  await seedQuarantineEvents(sessionCookie, null);

  console.log("[seed] Seed E2E (Sprint 6) concluído com sucesso.");
}

main().catch((err) => {
  console.error("[seed] ERRO:", err);
  process.exit(1);
});
