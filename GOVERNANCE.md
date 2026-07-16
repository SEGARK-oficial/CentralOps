# Governança do CentralOps

Este documento define **como o projeto é governado**: quem mantém, como as decisões
são tomadas, o que é (e o que nunca deixará de ser) gratuito e aberto, e os
compromissos que assumimos com quem usa e contribui para o CentralOps.

A linguagem vinculante deste documento sobre contribuições refere-se sempre à **licença
declarada no arquivo [`LICENSE`](LICENSE)** na raiz deste repositório — a **AGPLv3**.

---

## 1. Modelo open-core

O CentralOps adota o modelo **open-core** no padrão consolidado de projetos como
GitLab (`/ee`), Grafana e OpenCTI: um **núcleo (core) aberto e genuinamente útil**,
acompanhado de uma **edição Enterprise proprietária** que vive **fora** deste
repositório. O artefato Community **nunca** contém o código Enterprise.

### 1.1 As duas edições

| Edição | Onde | Licença | Conteúdo |
|---|---|---|---|
| **Community** | Este repositório (público) | **AGPLv3** (`LICENSE`) | Motor de ingestão, normalização OCSF (base), redução de volume (base), roteamento (14 sinks), detecção in-pipeline, UI base, SSO/OIDC + RBAC, KMS/Vault, redação de PII, push-ingestion, docs. (O piso vinculante destas garantias é a Charter — §2.1.) |
| **Enterprise** | Edição proprietária separada (não pública) | Comercial | Multi-tenancy hierárquica / reseller-MSSP, busca federada cross-org/assíncrona, audit & compliance cross-tenant (WORM), HA / fleet. Compilada em **artefato separado**, ativada por licença. |

Os módulos Enterprise vivem **fora** deste repositório, e o artefato Community **nunca** os
contém — é isso que torna o gate aplicável e a promessa da Charter (§2) verificável.

### 1.2 A dependência aponta sempre Enterprise → Core

A dependência é **unidirecional**: a Enterprise Edition depende do Core; **o Core nunca
importa código Enterprise**, com a única exceção de um *hook* de descoberta guardado
(import opcional com efeito colateral, que falha em silêncio para Community). Não há
acoplamento do Core para a edição paga — é isso que mantém o Core **100% funcional
sozinho** e a fronteira limpa.

### 1.3 Fronteira de contribuição (Community-only)

Contribuições neste repositório são **somente** para o Core Community. Código
proprietário ou de natureza Enterprise **nunca** deve ser adicionado aqui. A fronteira é
mantida por **duas camadas complementares**: um **gate automatizado** e a **revisão de
governança**. O gate não verifica "a fronteira" em abstrato; ele verifica um conjunto
**concreto e fechado** de invariantes estruturais.

**O que o gate automatizado verifica** (`backend/tests/test_open_core_boundary.py` e
`.github/workflows/openness-gate.yml`, rodando em todo PR):

- que **não** existe o pacote `centralops_ee` vendorado no artefato Community;
- que **não** existe o workspace de frontend `web-ee` vendorado;
- que o **único** import do overlay Enterprise no Core é o *hook* de descoberta guardado
  em `core/edition.py` (`activate_enterprise`) — e que, na ausência do EE, ele resolve
  para Community (fail-closed-to-Community);
- que **não** há dependência nem import de cobrança (Stripe) no backend Community;
- que **nenhuma chave privada** (PEM) está commitada na árvore, e que o keyring de
  licença embutido (`core/license_keys/`) só carrega material **público**;
- que a fonte Community do frontend **não** importa componentes EE (`web-ee`);
- que o **motor de busca federada** não está presente no core Community.

**O que o gate NÃO verifica — e por que isso importa.** O gate é estrutural, não
semântico. As **travas Enterprise da Charter** que não são pacotes/imports/arquivos
discretos — **multi-tenancy hierárquica/MSSP**, **audit & compliance cross-tenant** e
**HA / fleet** — **não** são detectáveis por um grep e **não** são impostas pelo gate.
Um PR poderia, em tese, adicionar lógica de auditoria cross-tenant ao core e **passar**
no gate. Essas travas são **política de governança imposta na revisão** (§2.2, §3.2),
não automação. O gate prova que o artefato Community está estruturalmente limpo de
material proprietário; a coerência da Charter quanto a *escopo de capacidade* é
responsabilidade dos mantenedores no merge.

Você pode rodar o gate localmente:

```bash
APP_ENV=test APP_MASTER_KEY=<chave-de-32+-caracteres> SESSION_SECURE_COOKIE=false \
  python -m pytest backend/tests/test_open_core_boundary.py
```

As regras de uso, setup de ambiente e o caminho de contribuição estão no
[`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## 2. Charter de garantias

Esta é a **promessa pública** do CentralOps: a linha entre o que é livre no core e o que
é trava Enterprise. Ela existe para que adoção, fork e construção em cima do core sejam
decisões seguras, sem medo de que a base seja fechada depois.

**Princípio do corte:** gateamos o que **escala com o tamanho e a maturidade da
organização** (multi-tenant, reseller, compliance cross-tenant, HA, busca federada
ativa). **Nunca** gateamos **higiene de segurança de base** — SSO/OIDC, RBAC,
criptografia/KMS, redação de PII e audit básico ficam grátis. Cobrar por SSO em um
produto **de segurança** seria uma contradição de marca (anti "SSO tax").

### 2.1 SEMPRE grátis / livre no core (Community)

Esta tabela é o **piso vinculante** da Charter — a lista de capacidades que **não
deixarão de ser** Community.

| Capacidade | Garantia |
|---|---|
| **1 organização (single-tenant)** | A base single-org/single-tenant é e permanece Community. |
| **SSO/OIDC + RBAC** | Login federado e controle de acesso por papel — sem "taxa de SSO". |
| **Ingestão (todas as fontes)** | Syslog, APIs, S3, Kafka, push de edge — incluindo a push-ingestion. |
| **Roteamento + 14 sinks** | Roteamento vendor-neutro para todos os destinos suportados. |
| **Detecção in-pipeline** | Detecção/correlação dentro do pipeline. |
| **Normalização OCSF (base)** | Normalização para OCSF na base. |
| **Redução de volume (base)** | Dedup / drop / sampling. |
| **KMS / Vault** | Gestão de segredos e criptografia. |
| **Redação de PII** | Redação fail-closed no pipeline. |
| **Queries salvas + busca síncrona local** | Busca local e queries salvas. |
| **Audit append-only (base)** | Trilha de auditoria básica, com retenção fixa. |

### 2.2 Trava Enterprise (edição proprietária, fora deste repo)

Esta tabela é a **lista autoritativa** do escopo Enterprise. Mudanças a ela seguem o
processo de governança de §3.2 (por PR, auditável no histórico).

| Capacidade | Por que é trava |
|---|---|
| **Multi-tenancy hierárquica / reseller (MSSP)** | A hierarquia de tenants e o programa de revenda escalam com o porte da organização. *Distingue-se do **org-scope de base**, que é Community — o que é Enterprise é a **hierarquia/isolamento avançado MSSP**, não o escopo por organização da base.* |
| **Busca federada cross-org / assíncrona** | Busca ativa que cruza organizações e fontes, assíncrona. |
| **Audit & compliance cross-tenant** | Auditoria tamper-evident/imutável (WORM), retenção longa, export assinado, escopo cross-tenant. *Distingue-se do **audit append-only de base** (§2.1), que é Community.* |
| **HA / fleet** | Alta disponibilidade, multi-node e orquestração de frota. |

### 2.3 Promessa anti bait-and-switch

Open-core honesto se diferencia de "open-washing" por compromissos verificáveis:

- **O que está aberto continua aberto.** Código publicado sob a AGPLv3 permanece sob essa
  licença — não se "des-publica" um commit. A Charter (§2.1) é o piso, não o teto, do que
  é livre.
- **DCO, não cessão de copyright.** Contribuições ao core entram sob o **Developer
  Certificate of Origin 1.1** (`Signed-off-by`), **não** sob CLA de cessão. Você mantém o
  copyright do que contribui; o core permanece sob a AGPLv3. **Não reaproveitamos**
  contribuições da comunidade dentro da edição Enterprise proprietária sem a sua
  permissão. O valor de M&A do projeto está no EE proprietário — que já é nosso por ser
  proprietário — então **não precisamos** de cessão sobre o core.
- **A licença do core não é uma rampa para um modelo mais fechado.** Versões futuras só
  mudam de licença com o consentimento dos contribuidores (consequência direta do DCO).

### 2.4 Telemetria

Se houver telemetria, ela é **opt-out** e **nunca bloqueante**. O pipeline **não para**
de ingerir, rotear ou processar se a telemetria estiver desabilitada ou indisponível.
Verificação de licença é local/offline (air-gapped suportado); **não há call-home
obrigatório**.

---

## 3. Manutenção e decisão

### 3.1 Mantenedores

O CentralOps é mantido pela **equipe da plataforma CentralOps** (SEGARK). Os
mantenedores são responsáveis por triagem, review, merge e releases, e por manter a
Charter (§2) coerente com o que o código de fato entrega.

### 3.2 Como as decisões são tomadas

- **Discussão técnica é aberta.** Propostas, dúvidas de design e RFCs acontecem no
  GitHub Discussions e em issues com a label apropriada.
- **A decisão final de merge é dos mantenedores.** Em um projeto open-core, manter a
  coerência da Charter exige que os mantenedores sejam o ponto de decisão sobre o que
  entra no Core. Isso é exercido com transparência, não como veto silencioso.
- **As travas de escopo de capacidade (multi-tenancy/MSSP, audit cross-tenant, HA) são
  impostas aqui, na revisão** — não pelo gate automatizado (§1.3).
- **Mudanças de escopo Community ↔ Enterprise** são mudanças de governança: ocorrem por
  PR contra a tabela §2.2, com histórico auditável. A direção é sempre **expandir o que é
  livre**, nunca encolher o piso da Charter (§2.3).

### 3.3 Fronteira de contribuição na prática

Bugfixes, performance, refactors, docs, UX, acessibilidade, novos collectors/destinos
(via registry de plugins) e pontos de extensão/hooks são **bem-vindos por PR direto**.
Para algo grande que recrie uma área Enterprise *dentro deste repositório*, abra antes
uma **RFC** (issue com a label `enterprise-scope`). Isso **não é uma proibição** — fazer
fork e implementar o que você quiser é seu direito sob a AGPLv3; a política governa apenas
a **direção** de contribuição *para este repo*, para não desperdiçar o seu tempo. O
caminho da RFC e o critério objetivo de "grande" estão no [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## 4. Releases, suporte e segurança

### 4.1 Cadência de releases

Releases seguem uma **cadência fixa e previsível**, anunciada no repositório. Não há
promessa de datas para features individuais; há promessa de **ritmo**.

### 4.2 Suporte — sem SLA na comunidade

Manter um OSS com tração é trabalho contínuo, e somos um time pequeno:

- **Sem SLA em issues da comunidade.** Não há garantia de tempo de resposta para issues
  ou PRs do Core. Fazemos *best-effort*.
- **Perguntas de uso vão para o GitHub Discussions**, não para Issues. Issues são para
  bugs reprodutíveis e propostas acionáveis.
- **Suporte com SLA é uma capacidade Enterprise** — quem precisa de garantia contratual
  de resposta contrata a edição Enterprise (**support@segark.com**).

### 4.3 Segurança

Vulnerabilidades são tratadas por **disclosure privado e coordenado**, nunca por issue
pública. O processo, os canais (GitHub Security Advisories / e-mail) e a janela de
divulgação coordenada estão em [`SECURITY.md`](SECURITY.md). Contato de segurança:
**support@segark.com**.

---

## 5. Licenciamento

- **Core.** A licença do core é a **AGPLv3** (GNU Affero General Public License v3.0),
  declarada no arquivo [`LICENSE`](LICENSE) na raiz deste repositório. O copyleft de rede
  da AGPLv3 desincentiva o relicenciamento por hyperscaler como SaaS sem reciprocidade.
- **Enterprise Edition.** Edição proprietária separada, sob licença comercial. A EULA
  (`EULA.md`) rege exclusivamente a edição Enterprise.
- **Contribuições.** Sob **DCO 1.1** (`Signed-off-by`), vinculadas à AGPLv3. Sem CLA, sem
  cessão de copyright (§2.3).

A AGPLv3 no core e o EE proprietário são as duas peças que, juntas, permitem manter a
Charter (§2) e financiar a manutenção do Core sem fechar a base.

---

### Referências

- [`README.md`](README.md) · [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`SECURITY.md`](SECURITY.md)
- Documentação: [docs.segark.com](https://docs.segark.com)
