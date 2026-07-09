# Contribuindo com o CentralOps

Obrigado pelo interesse em contribuir com o **CentralOps** — uma plataforma de
pipeline de dados de segurança (SDPP) vendor-neutra. Este documento descreve
como propor mudanças, rodar os testes, e o que você precisa saber antes de
abrir um Pull Request.

---

## 1. Bem-vindo + escopo

Contribuições neste repositório são feitas **exclusivamente sobre o núcleo
Community** do CentralOps. O CentralOps adota um modelo **open-core**: o núcleo
Community é desenvolvido aqui, em aberto; os recursos Enterprise (EE) vivem em
um repositório proprietário **separado** e **não fazem parte deste repositório**.

Concretamente, **nunca** adicione a este repositório:

- pacotes ou módulos Enterprise (por exemplo, qualquer pacote `centralops_ee`);
- um workspace de frontend `web-ee` ou imports de componentes EE no frontend;
- integrações de cobrança/billing (por exemplo, Stripe ou similares);
- chaves privadas ou segredos;
- o motor de busca federada cross-org (recurso EE).

Essa fronteira é uma **garantia do projeto** (a Charter de garantias — veja
[`GOVERNANCE.md`](GOVERNANCE.md)): o que está no núcleo Community permanece livre. Ela é
verificada automaticamente — veja [Fronteira open-core](#6-fronteira-open-core). PRs que
violem a fronteira serão recusados pelo gate de CI antes de qualquer review.

Se você quer propor um recurso que parece pertencer ao EE, abra uma discussão
**antes** de codar (veja [Modelo de suporte](#8-modelo-de-suporte)). Boa parte
do que importa — ingestão completa, roteamento, os 14 sinks, detecção
in-pipeline, normalização OCSF base, SSO/OIDC + RBAC, segredos via KMS/Vault e
push-ingestion — é, por garantia, parte do núcleo Community e bem-vinda aqui.

---

## 2. Pré-requisitos e setup

### Backend (Python 3.12)

O backend usa **Python 3.12** com um virtualenv em `backend/.venv`.

```bash
# a partir da raiz do repositório
python3.12 -m venv backend/.venv
source backend/.venv/bin/activate
pip install --upgrade pip
pip install -r backend/requirements.txt
# se existir um arquivo de dependências de desenvolvimento/teste, instale-o também:
# pip install -r backend/requirements-dev.txt
```

### Frontend (React 18 + Vite)

O frontend é **React 18 + Vite**, testado com **vitest**, gerenciado via `npm`.

```bash
# a partir da raiz do repositório
cd frontend
npm install
```

---

## 3. Como rodar os testes

> **Importante:** rode os testes a partir da **raiz do repositório** e use os
> caminhos `backend.app.*` nos imports de teste (não `app.*`). O pacote é
> resolvido a partir da raiz.

### Backend (pytest)

Os testes de backend exigem algumas variáveis de ambiente. `APP_MASTER_KEY`
deve ter **pelo menos 32 caracteres**.

```bash
source backend/.venv/bin/activate

APP_ENV=test \
APP_MASTER_KEY="<chave-de-no-minimo-32-caracteres>" \
SESSION_SECURE_COOKIE=false \
python -m pytest backend/
```

Para rodar um arquivo ou teste específico:

```bash
APP_ENV=test \
APP_MASTER_KEY="<chave-de-no-minimo-32-caracteres>" \
SESSION_SECURE_COOKIE=false \
python -m pytest backend/tests/test_open_core_boundary.py -v
```

### Frontend (vitest)

```bash
cd frontend
npm test          # roda a suíte vitest
npm run build     # garante que o build de produção compila
```

**Toda PR precisa ter os testes verdes** (backend e, quando o frontend for
tocado, vitest + build) antes de pedir review.

---

## 4. Conventional Commits

As mensagens de commit seguem o padrão **[Conventional Commits](https://www.conventionalcommits.org/pt-br/)**.
O formato é:

```
<tipo>(<escopo opcional>): <descrição imperativa e curta>
```

Tipos comuns: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `ci`,
`build`, `perf`. Exemplos:

```
feat(pipeline): adiciona sink ClickHouse com tiering por org
fix(ingest): corrige eventtime-ns negativo no buffer Redis
docs(contributing): esclarece fronteira open-core
```

---

## 5. Licença e DCO

### 5.1. Licença do núcleo

O núcleo Community é licenciado sob a **AGPLv3** (GNU Affero General Public License
v3.0), conforme o arquivo [`LICENSE`](LICENSE) — **a fonte da verdade** sobre a licença
em vigor.

Sua contribuição é licenciada **sob a licença do projeto, conforme indicado no
arquivo `LICENSE`** — exatamente como descrito na DCO (abaixo). Você não precisa
escolher nem citar uma licença na sua contribuição; ela acompanha a licença do
projeto.

### 5.2. DCO — sign-off OBRIGATÓRIO

Não usamos **CLA** e **não exigimos cessão/transferência de copyright**. Essa é
uma decisão deliberada: cessão de copyright sinaliza
"bait-and-switch", e projetos como GitLab e Chef migraram de CLA para DCO
justamente por isso. Você **mantém o copyright** das suas contribuições.

No lugar do CLA, usamos a **[Developer Certificate of Origin (DCO) 1.1](https://developercertificate.org/)**.
A DCO é uma afirmação leve de que você tem o direito de submeter aquele código
sob a licença do projeto (o texto oficial diz "*the open source license
indicated in the file*" — ou seja, é agnóstico à licença e acompanha o
`LICENSE`).

**Todo commit precisa ser assinado (sign-off).** Use a flag `-s` do git:

```bash
git commit -s -m "feat(pipeline): adiciona sink ClickHouse"
```

Isso adiciona automaticamente o trailer ao final da mensagem do commit:

```
Signed-off-by: Seu Nome <seu-email@exemplo.com>
```

O nome e o e-mail do trailer devem bater com os do seu `git config`
(`user.name` / `user.email`) e devem ser reais — não use pseudônimos anônimos.

Esqueceu o sign-off? Conserte antes de pedir review:

```bash
# último commit:
git commit --amend -s --no-edit

# vários commits — reaplica todos os commits do seu branch acima de origin/main
# adicionando o sign-off (é o comando que o próprio gate de DCO imprime ao falhar):
git rebase --signoff origin/main
```

Um gate de CI verifica o sign-off; PRs sem ele não serão mergeadas. Quando ele
falha, copie o comando `git rebase --signoff origin/main` da mensagem do gate.

---

## 6. Fronteira open-core

A garantia de que código Enterprise/proprietário **nunca** entra neste
repositório (garantia **C1** da Carta) é verificada por um gate automatizado:

- **Teste:** [`backend/tests/test_open_core_boundary.py`](backend/tests/test_open_core_boundary.py)
- **Workflow de CI:** [`.github/workflows/openness-gate.yml`](.github/workflows/openness-gate.yml)

O gate falha se detectar, entre outras coisas:

- um pacote `centralops_ee` (o único import permitido é o hook guardado em
  `backend/app/core/edition.py`, `activate_enterprise`);
- um workspace de frontend `web-ee`;
- integração de billing/Stripe;
- chaves privadas commitadas (o keyring embutido só carrega material público);
- imports de componentes de frontend EE (ex.: `@centralops/web-ee`,
  `PartnerTenantsPanel`, `AutoApprovePolicyModal`);
- o motor de busca federada (que pertence ao EE — veja o carve-out abaixo).

### 6.1. Onde fica a linha da busca federada

O código de query/correlação é **dividido**: parte permanece no núcleo Community, parte
é uma trava Enterprise. Antes de mexer em qualquer arquivo de query, detecção ou
correlação, saiba de que lado da linha ele cai.

**Permanece no núcleo Community (bem-vindo aqui):**

- os modelos ORM `QueryJob`, `CorrelationRule` e `Detection` (formas de dado
  entrelaçadas, p. ex. via `SearchResult.query_job_id`) e seus repositórios;
- a **execução por-provider** (os dialetos por fonte);
- a fila/worker `collect.query`;
- a **triagem de Detection** e a rota `/api/detections` — triagem é SOC base, e
  o scheduler Community também emite `Detection`; não é trava paga.

**É Enterprise — NÃO adicione a este repositório (vive só no `centralops_ee`):**

- o motor de orquestração federada e seus serviços:
  `services/query_service.py`, `services/query_quota.py`,
  `services/query_sigma.py`, `services/correlation_service.py`;
- as tasks de orquestração `collectors/query_tasks.py`;
- os routers `routers/query_jobs.py` e `routers/correlation_rules.py`
  (as rotas `/api/query-jobs` e `/api/correlation-rules` são montadas apenas
  pelo overlay EE);
- as fachadas de frontend `@/ee/*` (`frontend/src/ee/routes.tsx` e
  `frontend/src/ee/integrationDetailSlots.tsx`), que no Community **devem
  permanecer stubs vazios** (`eeRoutes` vazio; o slot não importa o
  `PartnerTenantsPanel`). O overlay EE as sobrescreve via alias no build dele.

Em caso de dúvida, **a fonte da verdade é o próprio teste**
(`test_open_core_boundary.py`): os casos `test_federated_query_engine_not_in_community`,
`test_core_app_keeps_detections_but_not_federated_query`,
`test_community_frontend_imports_no_ee_component` e
`test_community_frontend_ee_seams_are_stubs` codificam exatamente esta fronteira.

### 6.2. Rodando o gate localmente

> **Atenção — o gate não é apenas um scan estático.** Vários casos importam
> `backend.app.main` e `backend.app.core.edition`, ou seja, **dão boot no app**
> (ex.: `test_ee_absent_resolves_to_community`,
> `test_core_app_keeps_detections_but_not_federated_query`). Por isso o teste
> precisa das **mesmas variáveis de ambiente completas** da seção 3
> (`APP_ENV` / `APP_MASTER_KEY` ≥ 32 caracteres / `SESSION_SECURE_COOKIE`), do
> `backend/.venv` **ativo**, e de ser rodado **a partir da raiz do repositório**.
> Rodar de outro diretório ou sem o venv pode disparar as falhas conhecidas de
> "dual-root" / `no such table` (o primeiro import do pacote vence e congela o
> estado), que não têm relação com a fronteira em si.

```bash
source backend/.venv/bin/activate

APP_ENV=test \
APP_MASTER_KEY="<chave-de-no-minimo-32-caracteres>" \
SESSION_SECURE_COOKIE=false \
python -m pytest backend/tests/test_open_core_boundary.py -v
```

Se este teste falhar, sua mudança cruzou a fronteira open-core — reveja o
escopo (seções 1 e 6.1) antes de prosseguir.

---

## 7. Fluxo de Pull Request

1. **Faça um fork** e crie um branch a partir de `main` com nome descritivo
   (ex.: `feat/clickhouse-sink`, `fix/eventtime-ns`).
2. **Implemente** a mudança no núcleo Community, respeitando a fronteira
   open-core (seções 1 e 6).
3. **Adicione/ajuste testes** cobrindo o comportamento novo ou corrigido.
4. **Garanta tudo verde localmente:**
   - `python -m pytest backend/` (com as env vars da seção 3);
   - `backend/tests/test_open_core_boundary.py` (seção 6.2);
   - quando tocar o frontend: `npm test` + `npm run build`.
5. **Faça commits** em Conventional Commits e **com sign-off** (`git commit -s`).
6. **Abra a PR** contra `main`, descrevendo o quê e o porquê da mudança, e
   referenciando a discussão/issue relacionada quando houver.
7. **Review:** mantenedores revisam a PR. Espere feedback e iteração; mudanças
   maiores ou que toquem decisões de arquitetura podem pedir alinhamento prévio
   via um ADR ou Discussion.

PRs só são mergeadas com **CI verde** (testes + gate de openness + verificação
de DCO) e **aprovação de review**.

---

## 8. Modelo de suporte

Este é um projeto open source mantido sem garantias de SLA:

- **Sem SLA em issues.** Não há compromisso de tempo de resposta em issues
  abertas no GitHub.
- **Dúvidas de uso → GitHub Discussions.** Perguntas do tipo "como faço para…",
  configuração, dúvidas de arquitetura e propostas de ideia vão para
  **Discussions**, não para Issues. Reserve **Issues** para bugs reproduzíveis e
  pedidos de feature bem definidos.
- **Segurança → veja [`SECURITY.md`](SECURITY.md).** **Nunca** reporte
  vulnerabilidades em issues públicas. Use o canal de divulgação privada
  (GitHub Security Advisories / e-mail de segurança) descrito no `SECURITY.md`,
  que segue um modelo de divulgação coordenada.

Releases saem em cadência fixa. Bom proveito, e obrigado por contribuir!
